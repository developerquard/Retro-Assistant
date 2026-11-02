"""The two logs: one JSON line each, and a cap that keeps them readable.

These files are the only evidence there is when the packaged build misbehaves, so the
things worth pinning down are that a line is really one parseable object with a time
on it, that a log which grows past the cap is rotated to exactly one previous
generation instead of being truncated away, and that none of it can ever raise into
a command.
"""
import json

from core import config, log


def _sandbox(tmp_path, monkeypatch):
    """Points both logs at a temporary folder, with the write counter starting over."""
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    monkeypatch.setattr(log, "_writes", {})
    return tmp_path


def test_one_event_is_one_json_line_with_a_time(tmp_path, monkeypatch):
    folder = _sandbox(tmp_path, monkeypatch)
    log.event("session_ready", model="auto", background=False)
    text = (folder / "retro.log").read_text(encoding="utf-8")
    assert text.endswith("\n") and len(text.splitlines()) == 1
    line = json.loads(text)
    assert line["event"] == "session_ready"
    assert line["model"] == "auto" and line["background"] is False
    assert len(line["time"]) >= 19, "a line without a timestamp cannot be placed"


def test_an_action_and_an_event_do_not_share_a_file(tmp_path, monkeypatch):
    folder = _sandbox(tmp_path, monkeypatch)
    log.record({"tool": "open_app", "ok": True})
    log.event("dispatch", chars=12)
    assert json.loads((folder / "actions.log").read_text().splitlines()[0])["tool"] == "open_app"
    assert json.loads((folder / "retro.log").read_text().splitlines()[0])["event"] == "dispatch"


def test_a_log_past_the_cap_is_rotated_to_one_previous_generation(tmp_path, monkeypatch):
    """The newest lines stay in retro.log; the ones before them are in retro.log.1."""
    folder = _sandbox(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "LOG_MAX_BYTES", 300)
    monkeypatch.setattr(log, "CHECK_EVERY", 1)
    for number in range(40):
        log.event("tick", n=number)
    previous = folder / "retro.log.1"
    assert previous.exists(), "a log past the cap was left to grow for ever"
    assert previous.stat().st_size <= 300 + 200, "the previous file is capped too"
    current = [json.loads(line)["n"] for line in (folder / "retro.log").read_text().splitlines()]
    older = [json.loads(line)["n"] for line in previous.read_text().splitlines()]
    assert current, "the running log must keep the newest lines, not lose them"
    assert max(current) == 39, "the newest line is in the log that is being written"
    assert max(older) < max(current) and older, "the previous generation holds what came before"


def test_a_log_under_the_cap_is_never_rotated(tmp_path, monkeypatch):
    folder = _sandbox(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "LOG_MAX_BYTES", 10_000)
    monkeypatch.setattr(log, "CHECK_EVERY", 1)
    for number in range(20):
        log.event("tick", n=number)
    assert not (folder / "retro.log.1").exists()
    # The events are selected by name rather than counted by line: `core.log` is a
    # module-level sink, so a background thread left by another test in the same run
    # can land a line here, and counting every line made this assertion fail on an
    # unrelated writer instead of on a lost rotation. All twenty in order is the
    # property that matters - and a stricter one than "there are twenty lines".
    ticks = [json.loads(line)["n"] for line in (folder / "retro.log").read_text().splitlines()
             if json.loads(line).get("event") == "tick"]
    assert ticks == list(range(20))


def test_a_log_that_cannot_be_written_never_raises(tmp_path, monkeypatch):
    """A broken profile must not be able to break a command or a reply."""
    blocked = tmp_path / "not-a-folder"
    blocked.write_text("this is a file, so it cannot hold a folder")
    monkeypatch.setattr(config, "LOG_DIR", blocked / "Logs")
    monkeypatch.setattr(log, "_writes", {})
    log.event("session_ready")          # must simply do nothing
    log.record({"tool": "open_app"})
    assert blocked.is_file()


def test_the_event_name_is_first_and_never_the_line_text(tmp_path, monkeypatch):
    """What is logged about a note is its length - the log is not the memory."""
    folder = _sandbox(tmp_path, monkeypatch)
    log.event("memory_saved", chars=42, count=3)
    line = json.loads((folder / "retro.log").read_text().splitlines()[0])
    assert set(line) == {"time", "event", "chars", "count"}
    assert line["chars"] == 42
