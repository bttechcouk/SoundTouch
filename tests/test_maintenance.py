"""Weekly restart of clock speakers: schedule, target selection, clock detection."""
import datetime as dt
import threading
import types
import xml.etree.ElementTree as ET

import soundtouch_controller as stc

UTC = dt.timezone.utc
CFG = {"enabled": True, "day": 6, "time": "04:00", "tz": "Europe/London"}


def test_due_sunday_4am_london():
    # Sun 11 Oct 2026, BST: 04:00 London = 03:00 UTC
    assert stc.maintenance_due_key(CFG, dt.datetime(2026, 10, 11, 3, 0, tzinfo=UTC))
    assert not stc.maintenance_due_key(CFG, dt.datetime(2026, 10, 11, 4, 0, tzinfo=UTC))
    assert not stc.maintenance_due_key(CFG, dt.datetime(2026, 10, 10, 3, 0, tzinfo=UTC))  # Saturday


def test_disabled_never_due():
    assert not stc.maintenance_due_key({**CFG, "enabled": False},
                                       dt.datetime(2026, 10, 11, 3, 0, tzinfo=UTC))


def test_store_defaults_and_update(tmp_path):
    ms = stc.MaintenanceStore(tmp_path / "m.json")
    assert ms.get() == stc.MaintenanceStore.DEFAULTS
    ms.update(day=2, last_result={"Bedroom": "restarted (10.0.0.1)"})
    assert ms.get()["day"] == 2 and ms.get()["time"] == "04:00"


def test_has_clock_reads_capabilities():
    dev = stc.SoundTouchDevice("10.0.0.2")
    dev._get = lambda p, timeout=4: ET.fromstring(
        "<capabilities><clockDisplay>true</clockDisplay></capabilities>")
    assert dev.has_clock()
    dev2 = stc.SoundTouchDevice("10.0.0.3")
    dev2._get = lambda p, timeout=4: ET.fromstring("<capabilities><lightswitch>true</lightswitch></capabilities>")
    assert not dev2.has_clock()


class _Dev:
    def __init__(self, name, clock, playing=False, accepts=True):
        self.name, self.device_id, self.host = name, name.upper(), "10.0.0." + str(len(name))
        self._clock, self._playing, self._accepts, self.rebooted = clock, playing, accepts, False
    def has_clock(self): return self._clock
    def is_playing(self): return self._playing
    def reboot(self): self.rebooted = True; return self._accepts


def test_restart_targets_idle_clock_speakers_only(tmp_path):
    devs = [_Dev("Bedroom", True), _Dev("Dining", True, playing=True),
            _Dev("Kitchen", False), _Dev("Conserv", True, accepts=False)]
    app = types.SimpleNamespace(_lock=threading.Lock(), devices=devs, reboot_status={},
                                maintenance_store=stc.MaintenanceStore(tmp_path / "m.json"))
    app._rediscover = lambda pending: {did: "10.0.0.99" for did in pending}
    result = stc.AppState.restart_clock_speakers(app, "test")
    assert result == {"Bedroom": "restarted (10.0.0.99)", "Dining": "skipped (playing)",
                      "Conserv": "restart refused"}
    assert not devs[2].rebooted and not devs[1].rebooted
    saved = app.maintenance_store.get()
    assert saved["last_result"] == result and saved["last_run"]
