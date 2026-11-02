import pytest

from core.voice import classify, is_sleep


@pytest.mark.parametrize("heard,expected", [
    ("sleep", True),
    ("Retro, sleep", True),
    ("go to sleep", True),
    ("so jao", True),
    ("सो जाओ", True),
    ("open notepad", False),
    ("", False),
])
def test_is_sleep(heard, expected):
    assert is_sleep(heard) is expected


@pytest.mark.parametrize("heard,expected", [
    ("What time is it", "ignore"),          # asleep without the wake word
    ("", "ignore"),
    ("Retro", "wake"),
    ("retro.", "wake"),
    ("Retro, open notepad", "command"),
    ("Retro, sleep", "ignore"),             # already asleep
])
def test_asleep_only_reacts_to_the_wake_word(heard, expected):
    kind, payload = classify(False, heard)
    assert kind == expected
    if kind == "command":
        assert payload == "open notepad"


@pytest.mark.parametrize("heard,expected,payload", [
    ("open notepad", "command", "open notepad"),      # wake word is optional now
    ("What time is it", "command", "What time is it"),
    ("Retro, open notepad", "command", "open notepad"),
    ("Retro", "wake", None),
    ("sleep", "sleep", None),
    ("Retro, sleep", "sleep", None),
    ("go to sleep", "sleep", None),
])
def test_awake_takes_commands_and_can_be_slept(heard, expected, payload):
    kind, got = classify(True, heard)
    assert kind == expected
    assert got == payload


def test_sleep_while_awake_beats_a_command_inside_the_same_phrase():
    # "Retro, sleep" must sleep even though "sleep" also contains no command.
    assert classify(True, "Retro, sleep")[0] == "sleep"
    assert classify(False, "Retro, sleep")[0] == "ignore"
