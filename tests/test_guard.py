import pytest

from core.guard import check
from core.router import Match, route


@pytest.mark.parametrize("text", [
    "shut down the laptop",
    "restart my computer",
    "laptop band karo",
    "लैपटॉप बंद करो",
    "delete that file",
    "install chrome",
    "send a message to mom",
    "call rahul",
    "hello there",
    "How are you doing today",
    "kaise ho tum",
    "तुम कैसे हो",
    "What's the weather like tomorrow",
    "mausam kaisa hai",
    "आज मौसम कैसा है",
    "Tell me a joke",
    "what can you do",
    "Who is the prime minister of India",
])
def test_refused(text):
    assert check(text)


@pytest.mark.parametrize("text", [
    "what time is it",
    "battery kitni bachi hai",
    "open notepad",
    "search for the weather",
])
def test_guard_never_blocks_a_rule_match(text):
    assert isinstance(route(text), Match)


@pytest.mark.parametrize("text", [
    "gaana band karo",          # this is the media pause command, not a shutdown
    "music band karo",
    "म्यूजिक बंद करो",
    "laptop ka timer lagao",    # mentions the device but asks for a timer
    "what time is it",          # question-shaped, but a real command keyword is present
    "how much RAM is free",
    "search for the weather",   # the soft weather rule must not swallow this
])
def test_not_refused(text):
    assert check(text) is None
