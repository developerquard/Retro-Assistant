"""Which language a sentence is in - enough to give it the right voice.

The voice is chosen per sentence, not per session, because a reply is not always in
one language: an answer to a Hindi question can quote an English name, and Retro can
be asked to translate something. Script detection is used rather than a language
model because it is exact for the three languages this app supports, it costs
nothing, and it cannot be wrong about a sentence it has already read:

* **kana** (hiragana or katakana) is Japanese and nothing else;
* **CJK ideographs** without kana are read as Japanese too - the app has no Chinese
  voice, and Japanese is what kanji-only text is here;
* **Devanagari** is Hindi (or Marathi/Nepali, which the same voice reads);
* **Latin letters** are English - the language every voice on this machine speaks;
* anything with no letters at all (numbers, punctuation, a dash) returns None, and
  the caller uses the language of the sentence before it.
"""

import re

JAPANESE, HINDI, ENGLISH = "ja", "hi", "en"

_KANA = re.compile(r"[\u3041-\u3096\u30a1-\u30fa\u31f0-\u31ff]")
_IDEOGRAPHS = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_DEVANAGARI = re.compile(r"[\u0900-\u097f]")
_LATIN = re.compile(r"[A-Za-z]")

NAMES = {JAPANESE: "Japanese", HINDI: "Hindi", ENGLISH: "English"}


def detect(text):
    """The language of one sentence, or None when it carries no letters.

    The order matters: kana is checked before Devanagari and Latin, so a Japanese
    sentence with an English product name in it is still Japanese.
    """
    text = str(text or "")
    if _KANA.search(text) or _IDEOGRAPHS.search(text):
        return JAPANESE
    if _DEVANAGARI.search(text):
        return HINDI
    if _LATIN.search(text):
        return ENGLISH
    return None


def name(lang):
    """The language's name for a log line or a tool's output."""
    return NAMES.get(str(lang or "").lower(), str(lang or "unknown"))


def spoken(lang):
    """True for the languages this app speaks with a neural voice."""
    return str(lang or "").lower() in (JAPANESE, HINDI)


# Where a Japanese or Hindi sentence may be broken without breaking a word. Japanese
# has no spaces, so these are the only honest places: after a clause, never mid-word.
_BREAKS = ("、", "。", "！", "？", "，", "।", "،", ",", ";", " ")


def split_neural(text, limit=80):
    """A long Japanese/Hindi sentence as pieces short enough to speak soon.

    Speech time - and therefore the silence before the first word of a reply - grows
    with the characters handed to the neural voice. A paragraph read in one request is
    a paragraph of silence; the same paragraph in two or three pieces has its first
    sound out while the rest is still being synthesised, in parallel.
    """
    rest = str(text or "").strip()
    pieces = []
    while len(rest) > limit:
        cut = -1
        for mark in _BREAKS:
            cut = max(cut, rest.rfind(mark, 0, limit))
        if cut < limit * 0.4:
            cut = limit
        else:
            cut += 1
        piece = rest[:cut].strip()
        if piece:
            pieces.append(piece)
        rest = rest[cut:].strip()
    if rest:
        pieces.append(rest)
    return pieces or ([rest] if rest else [])
