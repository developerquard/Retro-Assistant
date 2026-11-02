"""The owner's memory: written to the desktop, recalled into every prompt.

The request was "let it save my preferences and info in my desktop only, so if it
finds out something's important it can save it and recall it if ever needed" - so
these check exactly that: one file in the desktop folder, nothing saved twice, a
recall that finds a fact by word, and a prompt block short enough to be free.

Everything is pointed at a temporary folder, so a real run's notes are never touched.
"""
import pytest

from core import memory

# Captured at import, before the fixture points the module at a temporary folder, so
# one test can still ask where the *real* memory file lives.
REAL_FOLDER = memory.folder


@pytest.fixture(autouse=True)
def desk(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "folder", lambda: tmp_path)
    memory.forget_all()
    yield tmp_path
    memory.forget_all()


def test_the_real_folder_is_the_desktop(monkeypatch):
    """"Save it in my desktop only" is a location, not a suggestion."""
    from pathlib import Path
    desktop = Path.home() / "Desktop"
    if not desktop.is_dir():
        pytest.skip("no desktop folder on this machine")
    monkeypatch.setattr(memory, "folder", REAL_FOLDER)
    assert memory.folder() == desktop
    assert memory.path().parent == desktop
    assert memory.path().name == "Retro memory.md"


def test_a_fact_is_saved_on_the_desktop_with_the_date(desk):
    saved, message = memory.remember("my sister is called Ria")
    assert saved
    assert memory.path() == desk / "Retro memory.md"
    entries = memory.entries()
    assert len(entries) == 1
    assert entries[0]["text"] == "my sister is called Ria"
    assert len(entries[0]["date"]) == 10 and "-" in entries[0]["date"]
    assert "Noted" in message, "the reply to a memory is one short line"
    assert "Ria" in memory.path().read_text(encoding="utf-8")


def test_the_same_fact_is_not_saved_twice():
    memory.remember("prefers short answers")
    saved, message = memory.remember("Prefers short answers!!")
    assert saved, "a duplicate is not an error"
    assert "already" in message
    assert len(memory.entries()) == 1


def test_nothing_gives_nothing_to_recall():
    assert memory.remember("") == (False, "I didn't catch anything to remember.")
    assert memory.entries() == []
    assert memory.prompt() == "", "an empty memory must not appear in a prompt at all"


def test_recall_finds_a_fact_by_word_and_keeps_the_newest_first():
    memory.remember("the music app is spotify")
    memory.remember("my sister is called Ria")
    memory.remember("prefers short answers")
    assert memory.recall("who is your sister") == ["my sister is called Ria"]
    assert memory.recall()[:2] == ["prefers short answers", "my sister is called Ria"]
    assert memory.recall("nothing about a car at all") == []


def test_the_prompt_block_shows_the_newest_and_tells_the_model_how_to_use_them():
    memory.remember("one")
    memory.remember("two")
    block = memory.prompt()
    assert "What you remember about your owner" in block
    assert block.index("two") < block.index("one"), "the newest line must be first"
    assert "never repeat them back as a list" in block


def test_the_prompt_stays_short_no_matter_how_much_is_saved():
    for number in range(20):
        memory.remember(f"fact number {number}")
    block = memory.prompt(max_lines=5)
    assert block.count("\n- ") == 5
    assert "fact number 19" in block and "fact number 14" not in block


def test_the_file_is_trimmed_rather_than_growing_for_ever(monkeypatch):
    monkeypatch.setattr(memory, "MAX_FILE_LINES", 4)
    for number in range(10):
        memory.remember(f"line {number}")
    kept = [entry["text"] for entry in memory.entries()]      # newest first
    assert len(kept) == 4, kept
    assert kept == ["line 9", "line 8", "line 7", "line 6"], "the oldest fell off"
    assert memory.path().read_text(encoding="utf-8").startswith("# What Retro knows")


def test_a_memory_that_cannot_be_written_says_so_instead_of_raising(monkeypatch, tmp_path):
    def explode():
        raise OSError("no desktop")

    monkeypatch.setattr(memory, "path", explode)
    saved, message = memory.remember("anything at all")
    assert not saved and "couldn't write" in message


def test_forget_removes_the_matching_note_and_leaves_the_rest(desk):
    """A wrong memory is worse than none, so it can be taken out again."""
    memory.remember("my sister is called Ria")
    memory.remember("prefers short answers")
    removed, message = memory.forget("forget that my sister is called Ria")
    assert removed == 1
    assert message == "Forgotten."
    assert [entry["text"] for entry in memory.entries()] == ["prefers short answers"]
    assert "Ria" not in memory.path().read_text(encoding="utf-8")


def test_forget_needs_every_word_so_it_cannot_take_a_wrong_note(desk):
    memory.remember("my sister is called Ria")
    memory.remember("my brother is called Tom")
    removed, message = memory.forget("my sister Ria")
    assert removed == 1, "only the sister note matches every word"
    assert "Tom" in memory.path().read_text(encoding="utf-8")


def test_forgetting_nothing_does_not_rewrite_the_file(desk):
    memory.remember("prefers short answers")
    before = memory.path().read_text(encoding="utf-8")
    removed, message = memory.forget("my birthday")
    assert removed == 0
    assert "don't have a note" in message
    assert memory.path().read_text(encoding="utf-8") == before, \
        "a failed forget must not lose a line"


def test_forget_with_no_words_asks_instead_of_guessing(desk):
    memory.remember("prefers short answers")
    assert memory.forget("") == (0, "Tell me which note to forget, and I'll remove it.")
    assert memory.forget("the of and")[0] == 0
    assert len(memory.entries()) == 1


def test_forget_says_how_many_when_several_match(desk):
    memory.remember("likes tea in the morning")
    memory.remember("likes tea after lunch")
    memory.remember("uses a blue notebook")
    removed, message = memory.forget("likes tea")
    assert removed == 2
    assert "2" in message
    assert [entry["text"] for entry in memory.entries()] == ["uses a blue notebook"]
