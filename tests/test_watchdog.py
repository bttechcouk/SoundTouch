"""Rediscovery watchdog: rescan when a known speaker stops answering (DHCP moves)."""
import threading
import types

import pytest

import soundtouch_controller as stc


class _Dev:
    def __init__(self, name, host, did, alive=True):
        self.name, self.host, self.device_id, self.alive = name, host, did, alive
    def ping(self, timeout=2): return self.alive


def _run(app, ticks, monkeypatch, clock):
    calls = {"n": 0}
    def sleep(_):
        calls["n"] += 1
        clock[0] += 60
        if calls["n"] > ticks: raise SystemExit
    monkeypatch.setattr(stc.time, "sleep", sleep)
    monkeypatch.setattr(stc.time, "monotonic", lambda: clock[0])
    with pytest.raises(SystemExit):
        stc.AppState._rediscovery_watchdog(app)


def test_rescans_when_a_speaker_stops_answering_rate_limited(monkeypatch):
    clock = [1000.0]
    dining = _Dev("Dining Room", "10.10.10.163", "D1", alive=False)
    app = types.SimpleNamespace(_lock=threading.Lock(), devices=[_Dev("Kitchen", "10.10.10.158", "K1"), dining])
    scans = []
    def scan():
        scans.append(clock[0])
        # the speaker turns up at its new address
        app.devices = [app.devices[0], _Dev("Dining Room", "10.10.10.126", "D1")]
    app.scan = scan
    _run(app, 3, monkeypatch, clock)
    assert len(scans) == 1                       # found it; no further rescans


def test_switched_off_speaker_rescans_at_most_every_five_minutes(monkeypatch):
    clock = [1000.0]
    app = types.SimpleNamespace(_lock=threading.Lock(), devices=[_Dev("Callum", "10.10.10.174", "C1", alive=False)])
    scans = []
    app.scan = lambda: scans.append(clock[0])     # still not found, stays listed
    _run(app, 12, monkeypatch, clock)              # ~11 minutes of checks
    assert 2 <= len(scans) <= 3                    # not every minute
    assert all(b - a > 300 for a, b in zip(scans, scans[1:]))


def test_all_answering_never_scans(monkeypatch):
    clock = [1000.0]
    app = types.SimpleNamespace(_lock=threading.Lock(), devices=[_Dev("Kitchen", "10.10.10.158", "K1")])
    app.scan = lambda: pytest.fail("should not rescan")
    _run(app, 5, monkeypatch, clock)
