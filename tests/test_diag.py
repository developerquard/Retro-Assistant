"""The latency recorder: real durations, absent stages, and nothing private.

These do not touch a microphone, a model or a voice. What they pin is the part a
developer relies on when a request is slow: a stage is measured where it happens,
the derived numbers (first text, first audio, total) come from those same marks,
a missing stage stays missing instead of becoming 0 ms, and the record written to
disk carries no words from the conversation.
"""
import csv
import json

import pytest

from core import config, diag


@pytest.fixture(autouse=True)
def clean_recorder(tmp_path, monkeypatch):
    """Every test gets its own history file and an empty ring."""
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "Logs")
    diag.reset()
    diag.set_enabled(True)
    yield
    diag.set_enabled(True)
    diag.reset()


def test_a_trace_measures_stages_marks_and_the_numbers_derived_from_them():
    import time

    trace = diag.new(kind="voice", origin="voice")
    trace.add("capture", 900_000_000)                 # 900 ms
    trace.add("stt", 150_000_000)
    with trace.span("route"):
        time.sleep(0.02)                              # a real interval, really measured
    trace.mark("first_text")
    with trace.span("llm"):
        time.sleep(0.03)
    trace.mark("first_audio")
    trace.mark("play_start")
    time.sleep(0.01)
    trace.mark("play_end")
    trace.model = "test-model"
    trace.streaming = True
    trace.finish(ok=True)

    item = trace.data()
    assert item["total_ms"] >= 50, "total must include the wall time that elapsed"
    assert item["stages"]["capture"]["ms"] == 900.0
    assert item["stages"]["stt"]["ms"] == 150.0
    assert 25.0 <= item["stages"]["llm"]["ms"] <= 200.0, "the span really measured"
    assert item["stages"]["route"]["calls"] == 1
    assert item["time_to_first_text_ms"] is not None
    assert item["time_to_first_audio_ms"] >= item["time_to_first_text_ms"]
    assert item["playback_ms"] is not None and item["playback_ms"] >= 0
    assert item["slowest"] == "capture"
    assert item["model"] == "test-model" and item["streaming"] is True
    assert item["ok"] is True


def test_repeated_calls_are_summed_and_counted():
    trace = diag.new()
    for value in (10_000_000, 20_000_000, 30_000_000):
        trace.add("llm", value)
    trace.finish()
    stage = trace.data()["stages"]["llm"]
    assert stage == {"ms": 60.0, "calls": 3}


def test_a_stage_that_did_not_run_is_absent_not_zero():
    trace = diag.new(kind="typed", origin="typed")
    trace.add("route", 2_000_000)
    trace.finish()
    item = trace.data()
    assert "stt" not in item["stages"], "no microphone means no STT stage at all"
    assert "connect" in item["unavailable"], "the absent split is named, not zeroed"
    assert "urllib" in item["unavailable"]["connect"]
    assert all(stage["ms"] > 0 for stage in item["stages"].values())


def test_a_failure_keeps_the_status_and_the_class_but_not_the_message():
    """A provider body can echo a request; the record may not carry it."""
    trace = diag.new()
    error = RuntimeError("HTTP 429: rate limit reached for key gsk_secret_value")
    trace.error("llm", error, status=429)
    trace.error("llm", RuntimeError("HTTP 500: bad"), status=500, retry=True)
    trace.finish(ok=False)
    item = trace.data()
    assert item["errors"][0]["status"] == 429
    assert item["errors"][0]["type"] == "RuntimeError"
    assert item["retries"] == 1
    blob = json.dumps(item)
    assert "gsk_secret_value" not in blob, "the exception message must not be stored"
    assert "rate limit reached" not in blob


def test_the_history_summary_and_report():
    for index, value in enumerate((100_000_000, 300_000_000, 200_000_000)):
        trace = diag.new(kind="voice", origin="voice")
        trace.add("stt", value)
        trace.add("route", 5_000_000)
        trace.mark("first_text")
        trace.mark("first_audio")
        trace.finish(ok=index != 1)
        diag.record(trace)
    stats = diag.summary()
    assert stats["count"] == 3
    # The stage averages are the measured 100/300/200; the totals are the wall time
    # these synthetic traces took to build, which is not the sum of stages a real
    # request measures - so they are asserted to exist, not to equal 205.
    assert stats["average_ms"] is not None
    assert stats["median_ms"] is not None
    assert stats["stages"]["stt"] == 200.0
    assert stats["slowest_stage"] == "stt"
    assert stats["failures"] == 1
    text = diag.report(limit=3)
    assert "latest" in text and "stt" in text
    assert "not measured" in text and "connect" in text
    assert "first audio" in text


def test_export_writes_json_and_csv(tmp_path):
    trace = diag.new(kind="typed", origin="typed", label="typed command")
    trace.add("route", 1_500_000)
    trace.add("llm", 250_000_000)
    trace.mark("first_text")
    trace.finish(ok=True)
    diag.record(trace)

    json_path = diag.export_json(tmp_path / "out.json")
    csv_path = diag.export_csv(tmp_path / "out.csv")
    assert json_path and json_path.exists()
    assert csv_path and csv_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["traces"][0]["id"] == trace.id
    assert "unavailable" in payload
    with open(csv_path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows and rows[0]["route_ms"] == "1.5"
    assert rows[0]["llm_ms"] == "250.0"
    assert rows[0]["stt_ms"] == "", "a stage that never ran is blank, not 0"


def test_history_survives_the_process_and_can_be_cleared(tmp_path):
    trace = diag.new(kind="probe", origin="stt", label="stt probe")
    trace.add("stt", 42_000_000)
    trace.finish()
    diag.record(trace)

    loaded = diag.history()
    assert len(loaded) == 1 and loaded[0]["stages"]["stt"]["ms"] == 42.0
    text = diag.report(items=loaded)
    assert "42" in text

    diag.clear_history()
    assert diag.history() == []


def test_the_recorder_can_be_turned_off_cheaply():
    diag.set_enabled(False)
    trace = diag.new()
    assert trace is None, "with recording off there is no object to fill in"
    diag.record(None)
    assert diag.recent() == []
    diag.set_enabled(True)
    assert diag.new() is not None


def test_the_current_trace_is_thread_local():
    import threading

    outer = diag.new(kind="outer", origin="typed")
    seen = []
    with diag.using(outer):
        seen.append(diag.current() is outer)

        def other():
            seen.append(diag.current() is None)
            inner = diag.new(kind="inner", origin="typed")
            with diag.using(inner):
                seen.append(diag.current() is inner)

        thread = threading.Thread(target=other)
        thread.start()
        thread.join()
    assert seen == [True, True, True]
    assert diag.current() is None


def test_a_session_request_records_one_trace():
    """A typed command with no voice still produces a measured request."""
    from core.session import Session

    session = Session(on_reply=lambda text, ok: None)
    session.fallback = None
    session.ears = None
    session.speaker = None
    result = session.ask("what time is it")
    assert result.ok
    recorded = [trace.data() for trace in diag.recent()]
    assert len(recorded) == 1
    item = recorded[0]
    assert item["origin"] == "typed"
    assert item["ok"] is True
    assert item["total_ms"] is not None
    assert "route" in item["stages"]
    assert item["model"] == "local"


def test_the_speaking_half_is_timed_too(monkeypatch):
    """First audio and playback start come from the voice's own workers.

    They are not measurable from the session: synthesis happens on one thread and
    playback on another, so the marks are taken where they happen. This uses the
    local-voice path with playback stubbed - no device, no sound - which is enough to
    prove the two instants are recorded.
    """
    import time

    import numpy as np

    from core import tts

    class Speaker:
        on_level = None
        device = None

        def prepare(self, text):
            return (16000, np.full(1600, 0.1, dtype="float32"))

        def say(self, text):
            return True

        def stop(self):
            return None

    trace = diag.new(kind="probe", origin="tts", label="tts probe")
    stream = tts.VoiceStream(speaker=Speaker(), allow_cloud=False, trace=trace)
    stream._play = lambda rate, audio: time.sleep(0.02)
    stream.feed("one short line")
    assert stream.finish(timeout=5), "the piece never finished playing"
    trace.mark("play_end")
    trace.finish(ok=True)

    item = trace.data()
    assert "tts" in item["stages"], "synthesis was not timed"
    assert item["time_to_first_audio_ms"] is not None
    assert item["playback_ms"] is not None and item["playback_ms"] >= 0
    # The two stages that are derived from marks rather than a span belong in the
    # same breakdown, or the panel and the CSV would show a blank for them.
    assert "buffering" in item["stages"]
    assert "playback" in item["stages"]
    assert item["stages"]["playback"]["ms"] == item["playback_ms"]
    assert item["stages"]["buffering"]["ms"] >= 0
