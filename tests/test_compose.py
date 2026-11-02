"""Writing something long: the brief, the outline, the sections, and the stop word.

The complaint this file pins down was specific. Ask Retro to write a documentary and
it went quiet for a minute, then read the whole thing out in one breath, with no
question about length and no sign that anything was happening. So these tests check
the three parts of the fix and nothing decorative:

* the request is recognised as a writing job (and a request to type a note into
  Notepad is not);
* one line is said first, and the user's answer decides the length - silence goes
  ahead at the proposed length rather than hanging;
* the document is written section by section, spoken as it is written, saved to the
  workspace, and stopped dead when the user says stop.

The other half of the same afternoon's work is here too: "stop" during a running
request no longer needs the wake word in front of it, and it now reaches a reply that
is still being streamed, instead of being noticed only at the end.
"""
import os
import re
import threading
import time

import pytest

from core import compose, files, progress, tts, voice
from core.control import Stopped, Task
from core.model import Result
from core.planner import Agent, parse_steps
from core.session import Session


# ---------- what counts as a writing job -------------------------------------

@pytest.mark.parametrize("text", [
    "write a documentary about the most popular anime",
    "draft an essay on black holes",
    "compose a short story about a lighthouse",
    "write me a report on last month's sales",
    "write a letter to my landlord about the leak",
    "prepare a guide to growing tomatoes",
])
def test_a_request_to_write_something_long_is_recognised(text):
    assert compose.looks_like_writing(text)


@pytest.mark.parametrize("text", [
    "write a note in notepad",
    "type this into word and save it",
    "make me a website",
    "open notepad and write hello there",
    "what time is it",
    "write",
    "send a message to mum",
])
def test_a_command_that_only_sounds_like_writing_is_not(text):
    assert not compose.looks_like_writing(text)


@pytest.mark.parametrize("answer,expected", [
    ("very short", 60),
    ("short please", 110),
    ("make it 350 words", 350),
    ("about two paragraphs", 210),
    ("one page", 400),
    ("two pages please", 800),
    ("long and detailed", 500),
    ("whatever you think", compose.DEFAULT_WORDS),
    ("", compose.DEFAULT_WORDS),
    ("9000 words", compose.MAX_WORDS),
])
def test_the_length_the_user_asked_for_is_the_length_written(answer, expected):
    assert compose.parse_length(answer) == expected


def test_a_spoken_length_is_readable_back():
    assert compose.describe(110) == "short, about 110 words"
    assert compose.parse_length(compose.describe(500)) == 500


# ---------- the outline ------------------------------------------------------

def test_an_outline_that_cannot_be_read_still_gives_the_document_a_shape():
    """A model that answers badly costs the shape, never the request."""
    title, sections = compose.parse_outline("", 240)
    assert title and sections
    assert sum(count for _heading, count in sections) < 900
    assert all(heading and count >= compose.MIN_SECTION_WORDS for heading, count in sections)


def test_an_outline_is_read_in_order_with_its_word_counts():
    title, sections = compose.parse_outline(
        "## The Cat Documentary\n- Origins | 60\n2. Why they purr | 80\n", 200)
    assert title == "The Cat Documentary"
    assert [heading for heading, _count in sections] == ["Origins", "Why they purr"]
    assert [count for _heading, count in sections] == [60, 80]


def test_a_section_with_no_word_count_still_gets_a_sensible_one():
    _title, sections = compose.parse_outline("Title\nOpening\nClosing", 200)
    assert [count for _heading, count in sections] == [compose.MIN_SECTION_WORDS] * 2


# ---------- the writer -------------------------------------------------------

class WritingModel:
    """A model that answers the outline prompt and the section prompts differently."""

    def __init__(self, title="Cats", sections=(("Origins", 60), ("Why they purr", 80))):
        self.title = title
        self.sections = sections
        self.prompts = []
        self.streamed = 0

    def __call__(self, messages, max_tokens=220):
        system = messages[0]["content"]
        self.prompts.append(system)
        if "Write the outline only" in system:
            return "\n".join([self.title] + [f"{h} | {w}" for h, w in self.sections])
        if "Write ONLY the section" in system:
            heading = re.search(r"the section '([^']+)'", system)
            name = heading.group(1) if heading else "it"
            return f"About {name}, there is this. And about {name}, there is that too."
        if "asks them how long it should be" in system:
            return ("On it - writing it now, and this may take a few seconds to a minute. "
                    "Would you like it short, around 110 words, or longer?")
        return ""          # any other line: say nothing, and let the caller fall back

    def stream(self, messages, max_tokens=400, on_delta=None):
        """Streams a section in two pieces, as a real cloud model would."""
        self.streamed += 1
        text = self(messages, max_tokens)
        half = text.index(".") + 1
        if on_delta is not None:
            on_delta(text[:half])
            on_delta(text[half:])
        return text


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "WORKSPACE", tmp_path)
    return tmp_path


def test_a_document_is_written_section_by_section_and_saved(workspace):
    model = WritingModel()
    events, sentences = [], []
    document = compose.write_document(
        request="write a documentary about cats", words=140, complete=model,
        stream=model.stream, on_sentence=sentences.append,
        on_event=lambda event: events.append(event))

    kinds = [event["kind"] for event in events]
    assert kinds[0] == "begin" and kinds[-1] == "done"
    assert kinds.count("section") == 2 and kinds.count("section_done") == 2
    # The sections are written in the order the outline gave them, and each one is
    # spoken as it is written rather than after the whole document exists.
    first = " ".join(sentences[:2])
    assert "Origins" in first and "purr" not in first
    assert "purr" in " ".join(sentences[2:])
    # The file is really there, with the whole document in it.
    written = (workspace / document["filename"]).read_text(encoding="utf-8")
    assert document["title"] in written and "## Origins" in written
    assert document["words"] == compose.count_words(written)
    assert document["words"] > 10
    assert document["filename"] in document["message"]


def test_a_section_is_streamed_so_it_can_be_spoken_while_it_is_written(workspace):
    """The first sentence of a section is heard before the section is finished."""
    model = WritingModel()
    seen = []
    stream_used = []

    def stream(messages, max_tokens=400, on_delta=None):
        stream_used.append(True)
        text = model(messages, max_tokens)
        half = text.index(".") + 1
        if on_delta is not None:
            on_delta(text[:half])
            seen.append(len(stream_used))          # what has been spoken so far
            on_delta(text[half:])
        return text

    compose.write_document(request="write about cats", words=140, complete=model,
                           stream=stream, on_sentence=lambda sentence: None)
    assert len(stream_used) == 2, "each section must be streamed, not composed whole"


def test_the_windows_view_of_the_document_grows_as_it_is_written(workspace):
    model = WritingModel()
    partials = []
    compose.write_document(request="write about cats", words=140, complete=model,
                           stream=model.stream, on_partial=partials.append,
                           on_event=lambda event: None)
    assert partials, "the window was never shown the text being written"
    assert all(isinstance(text, str) and text for text in partials)


def _stopped_after_one_section(model, task):
    """Writes a document and stops it the moment the first section is finished."""

    def watch(event):
        if event.get("kind") == "section_done" and event.get("index") == 1:
            task.cancel("user")                # the stop arrives mid-document

    return compose.write_document(request="write about cats", words=140, complete=model,
                                  stream=model.stream, on_sentence=lambda s: None,
                                  on_event=watch, task=task)


def test_stopping_a_document_keeps_the_sections_that_were_written(workspace):
    """A stop no longer throws the work away - which was this flow's weakest part.

    The document itself is still never half-saved: what is kept is a *draft*, and the
    stop reports how many sections are in it, because "your work is here" and "your
    work is gone" are the two different things a stop can mean.
    """
    model = WritingModel()
    document = _stopped_after_one_section(model, Task("write a documentary"))

    assert document["stopped"] and not document["filename"]
    assert not list(workspace.glob("*.md")), "a stopped document is not a saved document"
    assert document["done"] == 1 and "1 of 2" in document["message"], document["message"]
    assert "carry on writing" in document["message"]
    draft = compose.load_draft()
    assert draft is not None, "the finished section was thrown away"
    assert draft["title"] == "Cats" and len(draft["sections"]) == 1
    assert draft["sections"][0][0] == "Origins"
    assert draft["outline"] == [["Origins", 60], ["Why they purr", 80]]


def test_carrying_on_writes_the_rest_from_the_draft_and_saves_one_document(workspace):
    """"carry on writing": the length and the shape are kept, only the rest is written."""
    _stopped_after_one_section(WritingModel(), Task("write a documentary"))
    draft = compose.load_draft()
    model = WritingModel()
    events, sentences = [], []

    document = compose.write_document(request=draft["request"], words=draft["words"],
                                      complete=model, stream=model.stream,
                                      on_sentence=sentences.append,
                                      on_event=events.append, resume=draft)

    assert not document["stopped"] and document["finished"]
    assert events[0]["kind"] == "begin" and events[0]["resumed"] == 1
    written = (workspace / document["filename"]).read_text(encoding="utf-8")
    assert "## Origins" in written and "## Why they purr" in written
    # Only the missing section was asked for: the first sitting's work is not redone.
    asked = [prompt for prompt in model.prompts if "Write ONLY the section" in prompt]
    assert len(asked) == 1 and "Why they purr" in asked[0]
    assert compose.load_draft() is None, "a finished document has no draft left"
    assert "finished" in document["message"]


def test_the_draft_is_written_as_each_section_finishes_not_only_at_the_end(workspace):
    """A machine that dies mid-document still leaves the sections that were written."""
    model = WritingModel()
    seen = []

    def watch(event):
        seen.append((event["kind"], (compose.load_draft() or {}).get("sections")))

    compose.write_document(request="write about cats", words=140, complete=model,
                           stream=model.stream, on_sentence=lambda s: None,
                           on_event=watch)
    drafts = [sections for kind, sections in seen if kind == "section_done" and sections]
    assert [len(sections) for sections in drafts] == [1, 2], \
        "each finished section must be on disk before the next one is asked for"
    assert drafts[0][0][0] == "Origins"
    assert compose.load_draft() is None, "the finished document left its draft behind"


def test_the_documents_that_were_written_can_be_found_again(workspace):
    model = WritingModel()
    compose.write_document(request="write about cats", words=140, complete=model,
                           stream=model.stream, on_sentence=lambda s: None)
    found = compose.documents()
    assert len(found) == 1 and found[0]["name"].endswith(".md")
    assert found[0]["words"] > 10
    latest = compose.latest_document()
    assert latest["title"] == "Cats" and "## Origins" in latest["text"]
    assert compose.document_path(found[0]["name"]) == found[0]["path"]
    assert compose.document_path("../secrets.md") is None, "a name cannot leave the workspace"
    assert compose.document_path("nowhere.md") is None


def test_a_document_reads_as_speech_without_its_hash_marks(workspace):
    spoken = compose.readable("# Cats\n\n## Origins\n\nCats came from the desert. "
                             "They stayed.\n")
    assert "#" not in spoken and "Cats." in spoken and "Origins." in spoken
    assert compose.readable("") == "" and compose.readable("#\n\n- \n") == ""


@pytest.mark.parametrize("text", [
    "carry on writing",
    "Continue writing please",
    "Retro, finish the document",
    "resume writing",
    "carry on where you left off",
    "keep writing",
])
def test_a_request_to_carry_on_with_a_stopped_document_is_recognised(text):
    assert compose.is_resume(text)


@pytest.mark.parametrize("text", [
    "write a documentary about cats",
    "what is the weather",
    "continue",                      # on its own it could be anything; nothing is guessed
    "open notepad",
    "read my documents",             # that asks what exists, which is a list, not a reading
])
def test_a_request_that_merely_mentions_carrying_on_is_not(text):
    assert not compose.is_resume(text)


@pytest.mark.parametrize("text", [
    "read it back",
    "read that back to me",
    "read the last document",
    "read me what you wrote",
    "read the document out loud",
])
def test_a_request_to_hear_a_document_again_is_recognised(text):
    assert compose.is_readback(text)


def test_the_my_documents_tool_lists_real_documents_with_real_counts(workspace):
    from core import tools
    (workspace / "Cats.md").write_text("# Cats\n\nCats sleep all day.\n", encoding="utf-8")
    (workspace / "Dogs.md").write_text("# Dogs\n\nDogs run about.\n", encoding="utf-8")
    os.utime(workspace / "Cats.md", (1_000_000, 1_000_000))
    os.utime(workspace / "Dogs.md", (2_000_000, 2_000_000))
    result = tools.run("my_documents", {})
    assert result.ok and "Cats" in result.message and "Dogs" in result.message
    assert "5 words" in result.message, result.message
    assert str(workspace) in result.message, "the message must say where they are"
    assert "1 document(s)" in tools.run("my_documents", {"limit": 1}).message
    assert compose.documents(limit=1)[0]["name"] == "Dogs.md", "newest first"
    assert tools.run("my_documents", {"limit": 0}).ok, "a silly limit is not an error"


# ---------- the agent's own writing flow -------------------------------------

class StubSpeaker:
    def __init__(self, said=None):
        self.said = said if said is not None else []
        self.disabled = False
        self.playing = False
        self.level = 0.0
        self.device = None

    def say(self, text, cap=None):
        self.said.append(text)
        return True

    def say_async(self, text):
        self.said.append(text)
        return True

    def stop(self):
        return True


def test_the_brief_is_said_first_and_the_answer_sets_the_length(workspace):
    model = WritingModel()
    said = []
    agent = Agent(complete=model, speak=said.append, conversational=True, stream=model.stream)
    result = agent.compose("a documentary about cats", ask=lambda question, detail: "long please")
    assert result.ok
    assert said and "words" in said[0], f"no brief was spoken: {said}"
    outline_prompt = next(prompt for prompt in model.prompts if "Write the outline only" in prompt)
    assert "about 500" in outline_prompt, "the answer did not decide the length"


def test_silence_goes_ahead_at_the_length_retro_proposed(workspace):
    model = WritingModel()
    agent = Agent(complete=model, speak=lambda text: None, conversational=True,
                  stream=model.stream)
    result = agent.compose("a documentary about cats", ask=lambda question, detail: "")
    assert result.ok
    outline_prompt = next(prompt for prompt in model.prompts if "Write the outline only" in prompt)
    assert f"about {compose.DEFAULT_WORDS}" in outline_prompt


def test_no_microphone_means_no_question_and_no_hang(workspace):
    model = WritingModel()
    agent = Agent(complete=model, speak=lambda text: None, conversational=True,
                  stream=model.stream, brief=lambda question, detail: "")
    result = agent.compose("a documentary about cats")
    assert result.ok and model.prompts


def test_an_unanswered_question_says_which_length_it_went_with(workspace):
    """Silence is allowed, but it is not left mysterious: the choice is named out loud."""
    model = WritingModel()
    said = []
    agent = Agent(complete=model, speak=said.append, conversational=True,
                  stream=model.stream, brief=lambda question, detail: "")
    assert agent.compose("a documentary about cats").ok
    assert any(line.startswith("No answer, so I'll write it") for line in said), said
    assert any("words" in line for line in said if line.startswith("No answer")), said


def test_the_stop_line_says_what_was_kept_when_a_document_is_in_flight():
    """A stop is quiet in the mouth and complete on the screen.

    The owner asked for exactly this split: whatever is being said must be cut off,
    and the answer must be "Got it." (or nothing) - while the window still shows what
    survived the stop, because that is the fact worth keeping and speech is not the
    place to put it any more.
    """
    said, replies = [], []
    session = Session(on_reply=lambda text, ok: replies.append(text))
    session.speaker = StubSpeaker(said)
    session._writing_event({"kind": "begin", "title": "Cats", "total": 3, "words": 300})
    session._writing_event({"kind": "section_done", "index": 1, "total": 3, "words": 70})
    task = session._begin("write a documentary about cats")
    session.current_task = task
    session.steer(task, "stop")
    assert said == ["Got it."], f"a stop is one word, not a report: {said}"
    assert replies and "Cats" in replies[-1] and "1 of 3" in replies[-1], replies
    assert "carry on writing" in replies[-1]
    # And once the document is finished, a later stop is just a stop again.
    session._writing_event({"kind": "done", "title": "Cats", "words": 300, "file": "Cats.md"})
    assert session._stop_reply() == "Stopped."


def test_carry_on_writing_hands_the_draft_to_the_writing_flow(workspace):
    """The session, not the model, decides what "carry on writing" means."""
    compose.save_draft({"request": "a documentary about cats", "title": "Cats", "words": 140,
                        "outline": [["Origins", 60], ["Why they purr", 80]],
                        "sections": [["Origins", "Cats came from the desert."]],
                        "done": 1, "total": 2})
    seen = []
    session = Session()
    session.speaker = StubSpeaker()
    session.converse = True

    class Planner:
        conversational = True
        available = True

        def compose(self, text, task=None, ask=None, resume=None):
            seen.append((text, dict(resume or {})))
            return Result(True, "carried on")

        def run(self, text, task=None, note=None, mode=None):
            seen.append("planned")
            return Result(True, "planned")

    session.planner = Planner()
    task = session._begin("carry on writing")
    result = session._work(task, "carry on writing")
    assert result.ok, result.message
    assert seen and seen[0] != "planned", "carrying on was planned as a new request"
    assert seen[0][0] == "a documentary about cats", seen
    assert len(seen[0][1]["sections"]) == 1, "the draft was not handed over"


def test_carry_on_writing_with_no_draft_is_not_treated_as_a_resume(workspace):
    """With nothing stopped there is nothing to carry on with, so it becomes an ordinary
    request - which is how the user ends up being asked what they want written."""
    seen = []
    said = []
    session = Session()
    session.speaker = StubSpeaker(said)
    session.converse = True
    session.planner = Agent(complete=lambda messages, max_tokens=80: "",
                            speak=lambda text: None, conversational=True)

    def watch(text, task=None, emit=None):
        seen.append(text)
        return "Tell me what to write and I'll start it."

    session.planner.respond = watch
    task = session._begin("carry on writing")
    result = session._work(task, "carry on writing")
    assert seen == ["carry on writing"], "the request never reached the conversation"
    assert result.ok and "start it" in said[-1], said


def test_reading_a_document_back_speaks_the_file_sentence_by_sentence(workspace):
    (workspace / "Cats.md").write_text("# Cats\n\nCats sleep all day. They also decide "
                                       "when the day ends.\n", encoding="utf-8")
    said = []
    session = Session()
    session.speaker = StubSpeaker(said)
    session.converse = True

    class Planner:
        conversational = True
        available = True

        def run(self, text, task=None, note=None, mode=None):
            return Result(True, "planned")

    session.planner = Planner()
    task = session._begin("read it back")
    result = session._work(task, "read it back")
    assert result.ok and "Cats" in result.message and "words" in result.message
    assert any("Cats sleep all day." == line for line in said), said
    assert any(line.startswith("Cats.") for line in said), f"the title is read too: {said}"
    assert "#" not in " ".join(said)


def test_reading_back_with_nothing_written_says_so(workspace):
    said = []
    session = Session()
    session.speaker = StubSpeaker(said)
    session.converse = True
    session.planner = Agent(complete=lambda messages, max_tokens=80: "",
                            speak=session.say_step, conversational=True)
    task = session._begin("read it back")
    result = session._work(task, "read it back")
    assert result.ok and "haven't written" in result.message


def test_a_spoken_line_before_an_answer_does_not_make_the_answer_twice(workspace):
    """A `say` step then an `answer` step: the reply is heard once, not twice."""
    said = []
    session = Session()
    session.speaker = StubSpeaker(said)

    class Planner:
        conversational = True
        available = True

        def line(self, purpose, detail="", fallback=""):
            return fallback

        def run(self, text, task=None, note=None, mode=None):
            agent = Agent(complete=lambda messages, max_tokens=80: "",
                          speak=session.say_step, conversational=True)
            return agent._execute(text, [("say", {"text": "Opening it now."}),
                                        ("answer", {"text": "It is open, and that is that."})],
                                  None, note or (lambda *args: None))

    session.planner = Planner()
    request = "write the notes and then tell me"       # a job, so it goes to the agent
    task = session._begin(request)
    result = session._work(task, request)
    assert result.ok and "open" in result.message
    assert said.count("It is open, and that is that.") == 1, \
        f"the reply was spoken twice: {said}"
    assert said.count("Opening it now.") == 1


def test_a_compose_step_in_a_plan_runs_the_writing_flow(workspace, monkeypatch):
    """The model may choose it itself: `compose | subject | length`."""
    steps = parse_steps("compose | a documentary about cats | 300 words")
    assert steps == [("compose", {"subject": "a documentary about cats", "length": "300 words"})]
    called = []
    monkeypatch.setattr("core.tools.run",
                        lambda *args, **kwargs: called.append(args) or Result(True, "tool"))
    model = WritingModel()
    agent = Agent(complete=model, speak=lambda text: None, conversational=True,
                  stream=model.stream)
    monkeypatch.setattr(agent, "plan", lambda text, task=None, history=None: steps)
    result = agent.run("write a documentary about cats")
    assert result.ok and not called, "a compose step must not run a tool"
    assert list(workspace.glob("*.md")), "the document was never saved"


def test_chat_mode_refuses_to_write_a_document(workspace):
    model = WritingModel()
    agent = Agent(complete=model, speak=lambda text: None, conversational=True,
                  stream=model.stream)
    result = agent._execute("write a documentary about cats",
                            [("compose", {"subject": "cats", "length": "200 words"})],
                            None, lambda *args: None, mode="chat")
    assert not result.ok and "chat mode" in result.message
    assert not list(workspace.glob("*.md"))


class WholeBrain:
    """A brain with the two methods a real one has: ask, and stream."""

    def __init__(self, model):
        self.model = model

    def ask(self, messages, max_tokens=220):
        return self.model(messages, max_tokens)

    def stream(self, messages, max_tokens=400, on_delta=None):
        text = self.model(messages, max_tokens)
        if on_delta is not None:
            on_delta(text)
        return text

    def unload(self):
        return None


def test_a_writing_request_runs_end_to_end_through_the_session(workspace):
    """The whole path, as the app really drives it.

    session -> the agent it built for itself -> the brief -> the outline -> the
    sections, spoken as they are written -> the file on disk -> the window's card.
    Nothing here is stubbed except the model and the sound card.
    """
    events, said = [], []
    session = Session(on_writing=events.append)
    session.speaker = StubSpeaker(said)
    session.converse = True
    session.ears = None                       # no microphone, so no question is waited on
    session.fallback = WholeBrain(WritingModel())
    session.planner = session._new_planner()

    task = session._begin("write a documentary about the most popular anime")
    result = session._work(task, "write a documentary about the most popular anime")

    assert result.ok and result.message, result.message
    kinds = [event["kind"] for event in events]
    assert kinds[0] == "brief" and kinds[-1] == "done", kinds
    assert "begin" in kinds and kinds.count("section") == 2
    assert list(workspace.glob("*.md")), "the document was never saved"
    spoken = [line for line in said if "there is" in line]
    assert len(spoken) == 4, f"the document was not spoken as it was written: {said}"
    assert said[0] != result.message, "the reply repeated what was already said"


def test_a_writing_request_goes_to_the_writing_flow_not_the_planner(workspace):
    """The session decides, before any plan exists: this is a document, not a job."""
    routed = []
    session = Session()
    session.speaker = StubSpeaker()
    session.converse = True

    class Planner:
        conversational = True
        available = True

        def compose(self, text, task=None, ask=None):
            routed.append(text)
            return Result(True, "written")

        def run(self, text, task=None, note=None, mode=None):
            routed.append("agent")
            return Result(True, "planned")

    session.planner = Planner()
    task = session._begin("write a documentary about the most popular anime")
    result = session._work(task, "write a documentary about the most popular anime")
    assert result.ok and routed == ["write a documentary about the most popular anime"]


# ---------- stop, without the wake word and without waiting ------------------

def test_a_control_word_is_heard_without_the_wake_word_only_while_a_request_runs():
    assert voice.classify(False, "stop", control=lambda text: True) == ("command", "stop")
    assert voice.classify(False, "stop") == ("ignore", None), \
        "asleep and idle, a bare stop is still not a request"


def test_the_control_word_rule_is_the_control_words_and_nothing_else():
    session = Session()
    session.speaker = StubSpeaker()
    task = session._begin("write a documentary")
    try:
        assert session._control_word("stop")
        assert session._control_word("carry on")
        assert session._control_word("what are you doing")
        assert not session._control_word("and then the weather was nice")
    finally:
        task.finish()
    assert not session._control_word("stop"), "no request running, no control words"


def test_stop_reaches_a_reply_that_is_still_being_streamed():
    """A socket call cannot be interrupted, but the next token can be dropped."""
    session = Session()
    session.speaker = StubSpeaker()

    class Brain:
        def stream(self, messages, max_tokens=400, on_delta=None):
            for piece in ("Hello there. ", "This is the rest of it."):
                if on_delta is not None:
                    on_delta(piece)
            return "Hello there. This is the rest of it."

    session.fallback = Brain()
    task = session._begin("tell me something")
    session.current_task = task
    heard = []
    assert session._model_stream([], 20, heard.append)
    assert heard == ["Hello there. ", "This is the rest of it."]

    task.cancel("user")
    with pytest.raises(Stopped):
        session._model_stream([], 20, heard.append)


def test_a_job_gets_an_immediate_acknowledgement_and_a_command_does_not():
    said = []
    session = Session()
    session.speaker = StubSpeaker(said)
    session._work_started("open notepad and type hello there")
    assert said and said[-1] in progress.ACKNOWLEDGING, said
    session._work_started("what time is it")
    assert len(said) == 1, "a one-step command keeps its instant result and its silence"


def test_the_acknowledgement_never_repeats_twice_in_a_row():
    picks = [progress.acknowledgement() for _ in range(12)]
    assert all(first != second for first, second in zip(picks, picks[1:]))
    assert set(picks) <= set(progress.ACKNOWLEDGING)


def test_writing_with_no_microphone_never_waits_for_an_answer():
    session = Session()
    session.speaker = StubSpeaker()
    session.ears = None
    started = time.time()
    assert session._ask_brief("How long?") == ""
    assert time.time() - started < 0.5, "the brief must not block when nobody can answer"


# ---------- one voice at a time ----------------------------------------------

def test_the_two_playback_paths_share_one_lock():
    """The hosted voice and the local voice are two streams onto the same speakers."""
    import inspect

    assert "one_voice_at_a_time" in inspect.getsource(voice.Speaker._play)
    assert "one_voice_at_a_time" in inspect.getsource(tts.VoiceStream._play)


def test_two_playbacks_cannot_run_at_the_same_time():
    inside = []
    overlap = []

    def play(name):
        with voice.one_voice_at_a_time():
            inside.append(name)
            if len(inside) > 1:
                overlap.append(tuple(inside))
            time.sleep(0.15)
            inside.remove(name)

    threads = [threading.Thread(target=play, args=(name,)) for name in ("hosted", "local")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not overlap, f"two voices were playing at once: {overlap}"
