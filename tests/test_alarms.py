"""Alarm scheduling: timezone handling, day matching and the preset-play path.
No speaker hardware — devices are stubbed."""
import datetime as dt

import pytest

import soundtouch_controller as stc

UTC = dt.timezone.utc


def _alarm(**kw):
    a = {"id": "a1", "enabled": True, "time": "07:00", "days": [4], "tz": "Europe/London"}
    a.update(kw)
    return a


def test_bst_alarm_fires_at_local_seven_not_utc_seven():
    # Fri 2 Oct 2026 — BST (UTC+1). 07:00 London is 06:00 UTC.
    assert stc.alarm_due_key(_alarm(), dt.datetime(2026, 10, 2, 6, 0, 20, tzinfo=UTC)) == "a1_2026-10-02"
    assert stc.alarm_due_key(_alarm(), dt.datetime(2026, 10, 2, 7, 0, 20, tzinfo=UTC)) is None


def test_gmt_alarm_after_clocks_go_back():
    # Fri 6 Nov 2026 — GMT, so 07:00 London is 07:00 UTC
    assert stc.alarm_due_key(_alarm(), dt.datetime(2026, 11, 6, 7, 0, tzinfo=UTC)) == "a1_2026-11-06"


def test_day_matching_uses_local_date():
    # 23:30 Thu UTC is 00:30 Fri in London — a Friday 00:30 alarm must ring
    a = _alarm(time="00:30", days=[4])
    assert stc.alarm_due_key(a, dt.datetime(2026, 10, 1, 23, 30, tzinfo=UTC)) == "a1_2026-10-02"
    assert stc.alarm_due_key(_alarm(days=[3]), dt.datetime(2026, 10, 2, 6, 0, tzinfo=UTC)) is None


def test_disabled_alarm_never_due():
    assert stc.alarm_due_key(_alarm(enabled=False), dt.datetime(2026, 10, 2, 6, 0, tzinfo=UTC)) is None


def test_env_fallback_for_alarms_without_tz(monkeypatch):
    monkeypatch.setenv("SOUNDTOUCH_TZ", "Europe/London")
    a = _alarm(); del a["tz"]
    assert stc.alarm_due_key(a, dt.datetime(2026, 10, 2, 6, 0, tzinfo=UTC)) == "a1_2026-10-02"


def test_bad_tz_falls_back_without_crashing(monkeypatch):
    monkeypatch.delenv("SOUNDTOUCH_TZ", raising=False)
    assert stc.alarm_tz(_alarm(tz="Not/AZone")) is None


# ── play_preset: UPNP stations go through AVTransport, others the key ───────
def _dev(presets):
    dev = stc.SoundTouchDevice("10.0.0.7")
    dev.calls = []
    dev.get_presets_detail = lambda: presets
    dev.play_via_avt = lambda url: dev.calls.append(("avt", url)) or True
    dev._key = lambda k: dev.calls.append(("key", k))
    return dev


def test_play_preset_upnp_uses_avtransport():
    dev = _dev([{"id": "3", "source": "UPNP", "location": "http://h:8888/dlna/stream/heart"}])
    assert dev.play_preset(3)
    assert dev.calls == [("avt", "http://h:8888/dlna/stream/heart")]


def test_play_preset_other_sources_press_the_key():
    dev = _dev([{"id": "5", "source": "TUNEIN", "location": "/v1/playback/station/s24939"}])
    dev.play_preset(5)
    assert dev.calls == [("key", "PRESET_5")]


# ── firing: verify it played, retry once, record the outcome ────────────────
class _Store:
    def __init__(self): self.results = []
    def record_result(self, aid, at, result): self.results.append(result)


def _fire(playing_after):
    sched = stc.AlarmScheduler.__new__(stc.AlarmScheduler)
    sched.VERIFY_AFTER = 0
    sched._store = _Store()
    dev = _dev([{"id": "3", "source": "UPNP", "location": "http://h/dlna/stream/x"}])
    dev.name = "Bedroom"
    states = iter(playing_after)
    dev.is_playing = lambda: next(states)
    dev.set_volume = lambda v: dev.calls.append(("vol", v))
    sched._device = lambda alarm: dev
    sched._fire(_alarm(preset=3, volume=20))
    return sched._store.results, dev.calls


def test_fire_records_played():
    results, calls = _fire([True])
    assert results == ["played"]
    assert calls.count(("avt", "http://h/dlna/stream/x")) == 1
    assert calls[-1] == ("vol", 20)   # volume re-applied once it's playing


def test_fire_retries_once_then_reports_failure():
    assert _fire([False, True])[0] == ["played (retry)"]
    assert _fire([False, False])[0] == ["failed"]
