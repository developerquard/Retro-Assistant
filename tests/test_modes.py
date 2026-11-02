"""Chat mode and work mode: the switch has to reach the executor, not just the UI.

The user was explicit that this must not be a decorative toggle. So these tests
check the three places a mode has to be read: the phrases that switch it, the tool
gate in the executor, and the typed rules in the runner - a command that matches a
rule must not run in chat mode either.
"""
import pytest

from core import modes, runner, tools
from core.model import Result
from core.planner import Agent


@pytest.mark.parametrize("text", ["chat mode", "switch to chat mode", "Retro, switch to chat mode.",
                                  "talk mode", "conversation mode", "let's just talk",
                                  "just talk", "chit chat mode"])
def test_chat_mode_phrases_are_understood(text):
    assert modes.parse(text) == modes.CHAT


@pytest.mark.parametrize("text", ["work mode", "Retro, work mode", "switch to work mode",
                                  "execution mode", "start working", "task mode",
                                  "switch to execution mode"])
def test_work_mode_phrases_are_understood(text):
    assert modes.parse(text) == modes.WORK


@pytest.mark.parametrize("text", ["start working on the report", "open the chat",
                                  "work on the presentation", "open work mode window",
                                  "the execution of that was slow"])
def test_a_sentence_that_merely_mentions_the_words_changes_nothing(text):
    """A phrase has to be the whole sentence, or a mode flips by accident."""
    assert modes.parse(text) is None


@pytest.mark.parametrize("spoken,expected", [
    ("listen after retro", True),
    ("turn on follow up mode", None),          # not a phrase the setting answers to
    ("follow ups", True),
    ("keep listening", True),
    ("stop listening after retro", False),
    ("only the wake word", False),
    ("wake word only", False),
])
def test_the_listen_after_retro_setting_has_its_own_words(spoken, expected):
    assert modes.parse_followup(spoken) == expected


def test_chat_mode_allows_reading_and_refuses_changing():
    assert modes.allows_tool(modes.CHAT, tools.TOOLS["read_screen"])
    assert modes.allows_tool(modes.CHAT, tools.TOOLS["list_windows"])
    assert not modes.allows_tool(modes.CHAT, tools.TOOLS["type_text"])
    assert not modes.allows_tool(modes.CHAT, tools.TOOLS["open_app"])
    assert not modes.allows_tool(modes.CHAT, tools.TOOLS["app_function"])
    assert not modes.allows_tool(modes.CHAT, tools.TOOLS["run_command"])
    assert modes.allows_tool(modes.WORK, tools.TOOLS["type_text"])


def test_the_tool_gate_is_read_by_the_thing_that_runs_tools():
    """tools.run is the last door: a step that got there anyway is still refused."""
    result = tools.run("type_text", {"text": "hello"}, mode=modes.CHAT)
    assert not result.ok and "chat mode" in result.message
    result = tools.run("read_screen", {"limit": 5}, mode=modes.CHAT)
    assert "chat mode" not in result.message          # reading is allowed through


def test_the_agent_refuses_a_step_in_chat_mode_and_the_tool_never_runs(monkeypatch):
    ran = []
    monkeypatch.setattr(tools, "run", lambda name, args, **kw: ran.append(name) or Result(True, "x"))
    def complete(messages, max_tokens=300):
        # The line the model writes for the chat-mode refusal is a separate call; a
        # fake that answers every prompt with the plan would only test itself.
        asked = messages[0]["content"]
        return ("That needs work mode - shall I switch?"
                if "cannot touch the machine" in asked else "open_app | notepad")

    agent = Agent(complete=complete, conversational=True)
    result = agent.run("open notepad", mode=modes.CHAT)
    assert not result.ok
    assert "work mode" in result.message, "the model's own line is what is heard"
    assert ran == [], "no tool may run in chat mode"


def test_work_mode_runs_the_same_step(monkeypatch):
    ran = []
    monkeypatch.setattr(tools, "run",
                        lambda name, args, **kw: ran.append(name) or Result(True, "Opened Notepad."))
    agent = Agent(complete=lambda messages, max_tokens=300: "open_app | notepad",
                  conversational=False)
    result = agent.run("open notepad", mode=modes.WORK)
    assert result.ok and ran == ["open_app"]


def test_a_typed_rule_does_not_act_in_chat_mode(monkeypatch):
    """\"open notepad\" matches a typed rule, and the rule must not run either."""
    tried = []
    monkeypatch.setattr(runner, "execute",
                        lambda action, args, **kw: tried.append(action.name) or Result(True, "x"))
    result = runner.handle("open notepad", mode=modes.CHAT)
    assert not result.ok and "chat mode" in result.message
    assert tried == []


def test_the_refusal_in_chat_mode_is_written_by_the_model():
    """With a brain behind it, the sentence the user hears is the model's own."""
    said = []

    class Planner:
        available = True
        conversational = True

        def line(self, purpose, detail="", fallback=""):
            said.append(purpose)
            return "That one needs work mode - shall I switch?"

        def respond(self, text, task=None, emit=None):
            return "hello"

        def run(self, text, task=None, note=None, mode=None):
            return Result(False, "no")

    result = runner.handle("open notepad", planner=Planner(), converse=True, mode=modes.CHAT)
    assert not result.ok
    assert "work mode" in result.message
    assert said == ["chat"]


def test_a_status_question_is_recognised_but_does_not_switch_anything():
    assert modes.asks_status("which mode are you in?")
    assert modes.parse("which mode are you in?") is None


@pytest.mark.parametrize("text", ["repeat that", "Retro, say that again.", "what did you say",
                                  "repeat", "I didn't hear that", "pardon"])
def test_asking_for_a_line_again_is_understood(text):
    assert modes.parse_repeat(text)


@pytest.mark.parametrize("text", ["repeat after me: the report is late", "say that again to Tom",
                                  "open the repeat playlist", "repeat the command"])
def test_a_sentence_about_repeating_is_not_a_request_to_repeat(text):
    """Whole sentences only: a document request must not be swallowed by this."""
    assert not modes.parse_repeat(text)
