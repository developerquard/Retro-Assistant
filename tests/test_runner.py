import pytest

from core import config, os_ops
from core.model import Action, Result
from core.runner import execute, handle


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    ws = tmp_path / "RetroWorkspace"
    ws.mkdir()
    monkeypatch.setattr(config, "WORKSPACE", ws)
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "Logs")
    calls = []
    monkeypatch.setattr(os_ops, "press_key", lambda k: calls.append(("key", k)))
    monkeypatch.setattr(os_ops, "open_file", lambda p: calls.append(("open", p)))
    monkeypatch.setattr(os_ops, "open_url", lambda u: (calls.append(("url", u)), True)[1])
    return {"ws": ws, "calls": calls, "logs": tmp_path / "Logs"}


def never_ask(question):
    pytest.fail(f"should not have asked: {question}")


def test_read_action_runs_without_asking(sandbox):
    res = handle("What time is it?", confirm=never_ask)
    assert res.ok and res.verified


def test_volume_up_sends_key_and_is_not_marked_verified(sandbox):
    res = handle("Increase the volume.", confirm=never_ask)
    assert sandbox["calls"] == [("key", "vol_up")]
    assert res.ok and not res.verified


def test_unknown_text_does_nothing(sandbox):
    res = handle("make me a sandwich", confirm=never_ask)
    assert not res.ok and sandbox["calls"] == []


def test_note_is_written_and_never_overwritten(sandbox):
    handle("Create a note: first", confirm=never_ask)
    handle("Create a note: second", confirm=never_ask)
    files = sorted((sandbox["ws"] / "Notes").glob("*.txt"))
    assert len(files) == 2
    assert {f.read_text(encoding="utf-8").strip() for f in files} == {"first", "second"}


def test_note_without_text_writes_nothing(sandbox):
    res = handle("Create a note:", confirm=never_ask)
    assert not res.ok
    assert not (sandbox["ws"] / "Notes").exists() or not list((sandbox["ws"] / "Notes").glob("*.txt"))


def test_note_text_never_reaches_the_log(sandbox):
    handle("Create a note: secret plan details", confirm=never_ask)
    log_text = (sandbox["logs"] / "actions.log").read_text(encoding="utf-8")
    assert "secret plan details" not in log_text
    assert "text_chars" in log_text


def test_find_with_two_matches_opens_nothing_and_offers_choices(sandbox):
    (sandbox["ws"] / "Inbox").mkdir()
    (sandbox["ws"] / "Notes").mkdir()
    (sandbox["ws"] / "Inbox" / "plan.md").write_text("a")
    (sandbox["ws"] / "Notes" / "plan.md").write_text("b")
    res = handle("Find plan.md in RetroWorkspace.", confirm=never_ask)
    assert len(res.choices) == 2
    assert sandbox["calls"] == []


def test_find_with_one_match_opens_it(sandbox):
    (sandbox["ws"] / "roadmap.txt").write_text("x")
    handle("Find roadmap in RetroWorkspace.", confirm=never_ask)
    assert [c[0] for c in sandbox["calls"]] == ["open"]


def test_find_never_looks_outside_workspace(sandbox, tmp_path):
    (tmp_path / "secret_plan.txt").write_text("nope")
    res = handle("Find secret_plan in RetroWorkspace.", confirm=never_ask)
    assert not res.ok and res.choices == [] and sandbox["calls"] == []


def test_sensitive_action_runs_only_after_yes(sandbox):
    ran = []
    action = Action(name="demo_sensitive", tier="sensitive", keywords=(),
                    extract=lambda raw: {}, run=lambda a: (ran.append(1), Result(True, "done"))[1],
                    description="Delete a demo file")
    execute(action, {"target": "x.txt"}, confirm=lambda q: False)
    assert ran == []
    execute(action, {"target": "x.txt"}, confirm=lambda q: True)
    assert ran == [1]


def test_cancelled_sensitive_action_is_logged(sandbox):
    action = Action(name="demo_sensitive", tier="sensitive", keywords=(),
                    extract=lambda raw: {}, run=lambda a: Result(True, "done"), description="Delete")
    execute(action, {}, confirm=lambda q: False)
    assert "cancelled" in (sandbox["logs"] / "actions.log").read_text(encoding="utf-8")


def test_timer_can_be_started_and_cancelled(sandbox):
    assert handle("Set a timer for 1 minute.", confirm=never_ask).ok
    res = handle("Cancel the timer.", confirm=never_ask)
    assert res.ok
    assert not handle("Cancel the timer.", confirm=never_ask).ok
