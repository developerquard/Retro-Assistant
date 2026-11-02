"""How Retro talks: friendly, short enough, never a feature list, and it remembers.

Three complaints are pinned down here. It answered too formally and kept reminding
the owner what it could do ("I can open apps, search the web, set reminders...");
it did not carry the conversation forward; and its replies should be medium - a
little information, said plainly, then stop.

The `<remember>` mark is the fourth thing: this path has no tools, so a fact told in
conversation is kept by the model marking it in its own answer - and the mark must
never reach the voice, even when a streamed piece cuts it in half.
"""
import pytest

from core import memory
from core.planner import CHAT_SYSTEM, RULES, Agent, _RememberTagger, build_chat_messages


def test_the_chat_prompt_tells_it_not_to_advertise_itself():
    lower = CHAT_SYSTEM.lower()
    assert "never list what you can do" in lower
    assert "do you want anything else" in lower
    assert "never advertise a feature" in lower
    assert "no 'certainly'" in lower or "no certainly" in lower


def test_the_chat_prompt_asks_for_a_short_friendly_answer():
    lower = CHAT_SYSTEM.lower()
    assert "one or two short sentences" in lower
    assert "be a person, not a manual" in lower
    assert "you remember this conversation" in lower


def test_the_plan_prompt_keeps_answers_medium_and_lets_it_remember():
    # Wrapped for width in the prompt itself, so the checks read it as one paragraph.
    rules = " ".join(RULES.lower().split())
    assert "one or two sentences, three at most" in rules
    assert "never list what you can do" in rules
    assert "never end a reply" in rules
    assert "remember | the fact in one short line" in rules


@pytest.fixture
def desk(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "folder", lambda: tmp_path)
    memory.forget_all()
    yield tmp_path
    memory.forget_all()


def test_what_it_remembers_reaches_both_prompts(desk):
    from core.planner import build_messages
    memory.remember("prefers short answers")
    plan = build_messages("what is my favourite colour")[0]["content"]
    chat = build_chat_messages("what is my favourite colour")[0]["content"]
    assert "prefers short answers" in plan, "a memory the model never sees cannot be used"
    assert "prefers short answers" in chat


def test_a_question_already_asked_is_left_out_of_the_past():
    """The model repeating itself, at its source: its own earlier answer.

    Asked "Commands." with its own previous answer to "Commands." in its history, the
    live session's model replied with that answer almost word for word - "Open apps,
    set reminders, adjust volume." - because it was sitting right there in the prompt.
    The owner's words were "it repeats lines". A small model shown the same question
    and its own answer to it is copying, not answering.
    """
    history = [("Commands.", "I can open apps, set reminders."),
               ("And,", "Sure, what else?"),
               ("commands", "I can open apps, set reminders.")]
    turns = [message["content"] for message in build_chat_messages("Commands.", history)
             if message["role"] != "system"]
    assert turns == ["And,", "Sure, what else?", "Commands."], turns
    assert "I can open apps" not in " ".join(turns), "its own old answer came back round"


def test_with_no_notes_the_prompt_is_unchanged(desk):
    from core.planner import build_messages
    assert memory.prompt() == ""
    assert "What you remember" not in build_messages("x")[0]["content"]
    assert "What you remember" not in build_chat_messages("x")[0]["content"]


# ---------- the <remember> mark ----------------------------------------------


def scripted(*pieces):
    """A stream that hands the pieces over one at a time, as a real model does."""
    def stream(messages, max_tokens=400, on_delta=None):
        for piece in pieces:
            if on_delta is not None:
                on_delta(piece)
        return "".join(pieces)
    return stream


def test_a_remember_mark_is_saved_and_never_spoken(desk):
    agent = Agent(complete=lambda messages, max_tokens=0: "",
                  stream=scripted("Sure - your sister is Ria. ",
                                  "<remember>sister is Ria</remember>",
                                  " I'll remember that."),
                  conversational=True)
    heard = []
    reply = agent.respond("my sister is called Ria", emit=heard.append)
    assert "<remember" not in reply and "</remember" not in reply
    assert not any("<remember" in sentence for sentence in heard), "the mark was read aloud"
    assert [entry["text"] for entry in memory.entries()] == ["sister is Ria"]
    assert "Ria" in " ".join(heard), "the actual answer still arrives"


def test_a_mark_split_across_two_pieces_is_still_taken_out(desk):
    agent = Agent(complete=lambda messages, max_tokens=0: "",
                  stream=scripted("Noted. <remem", "ber>sister is Ria</remember>",
                                  " She is your sister."),
                  conversational=True)
    heard = []
    reply = agent.respond("remember my sister is Ria", emit=heard.append)
    assert heard == ["Noted.", "She is your sister."], heard
    assert reply == "Noted. She is your sister."
    assert [entry["text"] for entry in memory.entries()] == ["sister is Ria"]


def test_an_unfinished_mark_is_dropped_rather_than_read_out(desk):
    tagger = _RememberTagger()
    assert tagger.feed("I will keep this. <remember>sister is") == "I will keep this. "
    assert tagger.feed(" Ria") == "", "the mark is still open"
    assert tagger.flush() == "", "an unfinished mark is never spoken"
    assert tagger.facts == []


def test_a_mark_never_closing_does_not_swallow_the_answer(desk):
    tagger = _RememberTagger()
    spoken = tagger.feed("Here is the answer " + "<remember" + "x" * 800)
    assert "Here is the answer" in spoken
    assert "<remember" not in spoken
    assert tagger.flush() == ""


def test_nothing_in_the_tagger_returns_empty_input():
    tagger = _RememberTagger()
    assert tagger.feed("") == ""
    assert tagger.feed(None) == ""
    assert tagger.flush() == ""


# ---------- the conversation carries forward ---------------------------------


def test_the_last_exchanges_are_in_the_next_prompt(desk):
    spoken = []

    def complete(messages, max_tokens=0):
        return "I am the answer."

    agent = Agent(complete=complete, stream=scripted("I am the answer."),
                  conversational=True)
    agent.respond("my favourite colour is blue", emit=spoken.append)
    agent.respond("and my sister is Ria", emit=spoken.append)
    past = agent._past()
    assert [question for question, _answer in past] == ["my favourite colour is blue",
                                                        "and my sister is Ria"]
    messages = build_chat_messages("what is it", agent._past())
    roles = [message["role"] for message in messages]
    assert roles[:6] == ["system", "user", "assistant", "user", "assistant", "user"]
    assert messages[-1]["content"] == "what is it"


def test_forgetting_starts_a_new_conversation(desk):
    agent = Agent(complete=lambda messages, max_tokens=0: "ok",
                  stream=scripted("ok"), conversational=True)
    agent.respond("one")
    agent.forget()
    assert agent._past() == []
