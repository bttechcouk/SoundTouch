"""History (events.log): what gets written, and reading it back newest-first."""
import logging
import types

import pytest

import soundtouch_controller as stc


@pytest.fixture
def history(tmp_path, monkeypatch):
    """history_event() writing to a temp events.log; returns its path."""
    path = tmp_path / "events.log"
    logger = logging.getLogger(f"soundtouch.events.{tmp_path.name}")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    h = logging.FileHandler(path, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(h)
    monkeypatch.setattr(stc, "events", logger)
    yield path
    h.close()


def test_events_round_trip_newest_first(history):
    stc.history_event("alarm", "Bedroom", "'Wake up' (preset 1): played")
    stc.bar_event(types.SimpleNamespace(host="10.0.0.5", name="Living Room"), "source STANDBY → PRODUCT/TV")
    got = stc.read_history(path=history)
    assert [(e["kind"], e["who"], e["msg"]) for e in got] == [
        ("soundbar", "Living Room", "source STANDBY → PRODUCT/TV"),
        ("alarm", "Bedroom", "'Wake up' (preset 1): played"),
    ]
    assert got[0]["t"].endswith("+00:00")          # UTC — the UI shows local time


def test_filter_by_kind_and_limit(history):
    for i in range(5):
        stc.history_event("speaker", "Kitchen", f"event {i}")
    stc.history_event("alarm", "Bedroom", "fired")
    assert [e["msg"] for e in stc.read_history(kind="speaker", limit=2, path=history)] == ["event 4", "event 3"]
    assert len(stc.read_history(kind="alarm", path=history)) == 1


def test_tabs_and_newlines_cannot_break_a_line(history):
    stc.history_event("speaker", "Kit\tchen", "line one\nline two")
    (e,) = stc.read_history(path=history)
    assert e["who"] == "Kit chen" and e["msg"] == "line one line two"


def test_reads_rotated_files_and_skips_junk(tmp_path):
    path = tmp_path / "events.log"
    path.write_text("2026-11-12T10:00:00+00:00\talarm\tBedroom\tnew\nnot a history line\n")
    (tmp_path / "events.log.1").write_text("2026-11-01T10:00:00+00:00\talarm\tBedroom\told\n")
    assert [e["msg"] for e in stc.read_history(path=path)] == ["new", "old"]


def test_missing_file_is_empty(tmp_path):
    assert stc.read_history(path=tmp_path / "nope.log") == []
