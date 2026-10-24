"""Rediscovery watchdog: rescan when a known speaker stops answering (DHCP moves)."""
import threading
import types

import pytest

import soundtouch_controller as stc


class _Dev:
    def __init__(self, name, host, did, alive=True):
        self.name, self.host, self.device_id, self.alive = name, host, did, alive
    def ping(self, timeout=2): return self.alive


def _app(devices, known=None, add=None):
    app = types.SimpleNamespace(_lock=threading.Lock(), devices=devices,
                                _known=known if known is not None else {d.device_id: (d.host, d.name) for d in devices})
    app.add_device = add or (lambda host: None)
    return app


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
    app = _app([_Dev("Kitchen", "10.10.10.158", "K1"), dining])
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
    app = _app([_Dev("Callum", "10.10.10.174", "C1", alive=False)])
    scans = []
    app.scan = lambda: scans.append(clock[0])     # still not found, stays listed
    _run(app, 12, monkeypatch, clock)              # ~11 minutes of checks
    assert 2 <= len(scans) <= 3                    # not every minute
    assert all(b - a > 300 for a, b in zip(scans, scans[1:]))


def test_all_answering_never_scans(monkeypatch):
    clock = [1000.0]
    app = _app([_Dev("Kitchen", "10.10.10.158", "K1")])
    app.scan = lambda: pytest.fail("should not rescan")
    _run(app, 5, monkeypatch, clock)


def test_dropped_speaker_is_re_added_from_its_last_address(monkeypatch):
    # The Bedroom fell out of the list (a scan missed it) but still answers
    # at its reserved address — the watchdog puts it back without a rescan.
    clock = [1000.0]
    kitchen = _Dev("Kitchen", "10.10.10.128", "K1")
    app = _app([kitchen], known={"K1": ("10.10.10.128", "Kitchen"), "B1": ("10.10.10.129", "Bedroom")})
    def add(host):
        d = _Dev("Bedroom", host, "B1"); app.devices.append(d); return d
    app.add_device = add
    app.scan = lambda: pytest.fail("should not need a rescan")
    _run(app, 3, monkeypatch, clock)
    assert [d.name for d in app.devices] == ["Kitchen", "Bedroom"]


def test_unplugged_known_speaker_rescans_only_every_30_minutes(monkeypatch):
    clock = [1000.0]
    app = _app([_Dev("Kitchen", "10.10.10.128", "K1")],
               known={"K1": ("10.10.10.128", "Kitchen"), "C1": ("10.10.10.131", "Callum")})
    scans = []
    app.scan = lambda: scans.append(clock[0])
    _run(app, 70, monkeypatch, clock)               # ~70 minutes
    assert 2 <= len(scans) <= 3
    assert all(b - a > 1800 for a, b in zip(scans, scans[1:]))


def test_scan_keeps_a_known_speaker_the_sweep_missed(monkeypatch):
    slow = _Dev("Bedroom", "10.10.10.129", "B1")            # answers a direct ping
    off  = _Dev("Callum", "10.10.10.131", "C1", alive=False)
    kitchen = _Dev("Kitchen", "10.10.10.128", "K1")
    app = types.SimpleNamespace(_lock=threading.Lock(), devices=[kitchen, slow, off], _known={},
                                store=types.SimpleNamespace(load_backup=lambda h: None))
    monkeypatch.setattr(stc, "discover_all", lambda timeout=3: [_Dev("Kitchen", "10.10.10.128", "K1")])
    stc.AppState.scan(app)
    assert sorted(d.name for d in app.devices) == ["Bedroom", "Kitchen"]
    assert set(app._known) == {"K1", "B1"}
