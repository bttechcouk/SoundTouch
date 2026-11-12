"""Dialogue mode: transition rules, /audiodspcontrols parsing, support probe
and the per-speaker auto setting. No speaker hardware — _get / _post / the
HTTP session are stubbed."""
import types
import xml.etree.ElementTree as ET

import pytest

import soundtouch_controller as stc


# ── audio_mode_for_transition ────────────────────────────────────────────────
@pytest.mark.parametrize("prev, cur, want", [
    ("BLUETOOTH",      "PRODUCT",   "dialog"),   # music → TV
    ("STANDBY",        "PRODUCT",   "dialog"),   # TV switched on, bar wakes
    ("INVALID_SOURCE", "PRODUCT",   "dialog"),
    ("PRODUCT",        "SPOTIFY",   "normal"),   # TV → music
    ("STANDBY",        "BLUETOOTH", "normal"),   # wake straight into music
    ("PRODUCT",        "PRODUCT",   None),       # TV↔HDMI share PRODUCT: leave manual choice
    ("SPOTIFY",        "BLUETOOTH", None),       # music → music: leave manual choice
    ("PRODUCT",        "STANDBY",   None),       # going to sleep: nothing to do
    (None,             "PRODUCT",   None),       # first sighting never acts
])
def test_transition(prev, cur, want):
    assert stc.audio_mode_for_transition(prev, cur) == want


# ── get/set audio mode ───────────────────────────────────────────────────────
ST300_DSP = ('<audiodspcontrols audiomode="{}" videosyncaudiodelay="0" '
             'supportedaudiomodes="AUDIO_MODE_NORMAL|AUDIO_MODE_DIALOG" />')


def _dev(xml_text):
    dev = stc.SoundTouchDevice("192.168.1.60")
    dev._get = lambda path, timeout=4: ET.fromstring(xml_text) if xml_text else None
    return dev


def test_get_audio_mode_parses():
    assert _dev(ST300_DSP.format("AUDIO_MODE_DIALOG")).get_audio_mode() == "dialog"
    assert _dev(ST300_DSP.format("AUDIO_MODE_NORMAL")).get_audio_mode() == "normal"
    assert _dev(None).get_audio_mode() is None
    assert _dev("<errors><error/></errors>").get_audio_mode() is None


def test_set_audio_mode_body():
    dev = stc.SoundTouchDevice("192.168.1.60")
    sent = []
    dev._post = lambda path, body, timeout=4: sent.append((path, body)) or True
    dev.set_audio_mode("dialog"); dev.set_audio_mode("normal")
    assert sent == [("/audiodspcontrols", '<audiodspcontrols audiomode="AUDIO_MODE_DIALOG"/>'),
                    ("/audiodspcontrols", '<audiodspcontrols audiomode="AUDIO_MODE_NORMAL"/>')]


# ── supports_dialog_mode probe ───────────────────────────────────────────────
class _Resp:
    def __init__(self, code, text): self.status_code, self.text = code, text


class _Session:
    def __init__(self, result): self.result, self.calls = result, 0
    def get(self, url, timeout=4):
        self.calls += 1
        if isinstance(self.result, Exception): raise self.result
        return self.result


def _probe_dev(result):
    dev = stc.SoundTouchDevice("192.168.1.60")
    dev._session = _Session(result)
    return dev


def test_probe_soundbar_supported_and_cached():
    dev = _probe_dev(_Resp(200, ST300_DSP.format("AUDIO_MODE_NORMAL")))
    assert dev.supports_dialog_mode() and dev.supports_dialog_mode()
    assert dev._session.calls == 1


def test_probe_404_is_permanent():
    dev = _probe_dev(_Resp(404, "<html>Object Not Found</html>"))
    assert not dev.supports_dialog_mode() and not dev.supports_dialog_mode()
    assert dev._session.calls == 1 and dev._dsp_supported is False


def test_probe_network_error_retries_later():
    dev = _probe_dev(ConnectionError("down"))
    assert not dev.supports_dialog_mode()
    assert dev._dsp_supported is None          # not cached as unsupported
    assert not dev.supports_dialog_mode()       # within back-off: no new request
    assert dev._session.calls == 1
    dev._dsp_checked -= 601
    dev._session.result = _Resp(200, ST300_DSP.format("AUDIO_MODE_DIALOG"))
    assert dev.supports_dialog_mode()


# ── AudioModeStore ───────────────────────────────────────────────────────────
def test_store_defaults_on_and_keys_by_device_id(tmp_path):
    store = stc.AudioModeStore(tmp_path / "audio_mode.json")
    dev = stc.SoundTouchDevice("10.0.0.5"); dev.device_id = "C4F31264FB9F"
    assert store.auto_enabled(dev) is True
    store.set_auto(dev, False)
    moved = stc.SoundTouchDevice("10.0.0.99"); moved.device_id = "C4F31264FB9F"
    assert store.auto_enabled(moved) is False   # survives a DHCP IP change


# ── TV wake (bar woken by the TV over CEC into its last music source) ────────
@pytest.mark.parametrize("woke, elapsed, source, playing, want", [
    ("SPOTIFY", 2,  "SPOTIFY", False, "wait"),    # give a real wake time to start
    ("SPOTIFY", 8,  "SPOTIFY", False, "switch"),  # silent after the grace → TV woke it
    ("SPOTIFY", 3,  "SPOTIFY", True,  "done"),    # Spotify Connect started playing
    ("UPNP",    9,  "UPNP",    True,  "done"),    # radio started from the app
    ("SPOTIFY", 4,  "PRODUCT", False, "done"),    # bar switched to TV by itself
    ("SPOTIFY", 9,  "STANDBY", False, "done"),    # turned off again
])
def test_tv_wake_action(woke, elapsed, source, playing, want):
    assert stc.tv_wake_action(woke, elapsed, source, playing, grace=8) == want


# ── lip-sync delay: remembered and restored on the TV input ──────────────────
def test_av_delay_store_roundtrip_keeps_auto_tv(tmp_path):
    store = stc.AudioModeStore(tmp_path / "audio_mode.json")
    dev = types.SimpleNamespace(device_id="C4F31264FB9F", host="10.0.0.5")
    assert store.av_delay(dev) is None
    store.set_auto(dev, False)
    store.set_av_delay(dev, 40)
    assert store.av_delay(dev) == 40
    assert store.auto_enabled(dev) is False
    store.set_av_delay(dev, 0)
    assert store.av_delay(dev) == 0


@pytest.mark.parametrize("prev,source,saved,expected", [
    ("STANDBY", "PRODUCT", 40, 40),     # TV on after the bar slept
    ("SPOTIFY", "PRODUCT", 40, 40),     # music → TV
    ("STANDBY", "PRODUCT", 0, 0),       # an explicit 0 is restored too
    ("PRODUCT", "PRODUCT", 40, None),   # staying on TV: leave manual changes
    ("STANDBY", "SPOTIFY", 40, None),   # music is never delayed
    ("PRODUCT", "STANDBY", 40, None),
    (None, "PRODUCT", 40, None),        # controller start-up
    ("STANDBY", "PRODUCT", None, None), # never set
])
def test_av_delay_for_transition(prev, source, saved, expected):
    assert stc.av_delay_for_transition(prev, source, saved) == expected


# ── saved vs live lip-sync delay shown in the UI ─────────────────────────────
def test_merge_saved_av_delay_shows_saved_value():
    boom = lambda: pytest.fail("source should not be read when nothing differs")
    assert stc.merge_saved_av_delay({"av_delay": 10, "cec": True}, 10, boom) == \
        {"av_delay": 10, "cec": True}
    assert stc.merge_saved_av_delay({"av_delay": 0}, None, boom) == {"av_delay": 0}
    assert stc.merge_saved_av_delay({"cec": True}, 10, boom) == {"cec": True}


@pytest.mark.parametrize("source,expected", [
    ("STANDBY", {"av_delay": 10}),                      # asleep: the bar just reports 0
    ("SPOTIFY", {"av_delay": 10}),
    ("PRODUCT", {"av_delay": 10, "av_delay_live": 0}),  # on TV and really different
])
def test_merge_saved_av_delay_flags_real_difference_on_tv(source, expected):
    assert stc.merge_saved_av_delay({"av_delay": 0}, 10, lambda: source) == expected


# ── soundbar event log ───────────────────────────────────────────────────────
def _bar(dsp):
    """Fake soundbar whose /audiodspcontrols answers from the mutable `dsp`."""
    def _get(path):
        assert path == "/audiodspcontrols"
        return ET.fromstring(f'<audiodspcontrols audiomode="{dsp["mode"]}" '
                             f'videosyncaudiodelay="{dsp["delay"]}"/>')
    return types.SimpleNamespace(host="10.0.0.5", name="Lounge", _get=_get,
                                 capabilities=lambda: {"audiodspcontrols"})


def test_record_bar_state_logs_changes_but_not_standby(monkeypatch):
    seen = []
    monkeypatch.setattr(stc, "bar_event", lambda dev, msg, tag="BAR": seen.append(msg))
    dsp = {"mode": "AUDIO_MODE_NORMAL", "delay": 10}
    dev, dsp_seen, dsp_next = _bar(dsp), {}, {}
    rec = lambda prev, src, acct="": stc.AppState._record_bar_state(
        None, dev, prev, src, acct, dsp_seen, dsp_next)

    rec("STANDBY", "PRODUCT", "TV")                 # wakes: source + state on wake
    assert seen == ["source STANDBY → PRODUCT/TV", "audio mode normal, lip-sync delay 10 ms"]

    seen.clear(); dsp["delay"] = 0
    rec("PRODUCT", "PRODUCT", "TV")                 # within 10 s: not re-read
    assert seen == []
    dsp_next.clear()
    rec("PRODUCT", "PRODUCT", "TV")                 # a real reset while on TV
    assert seen == ["lip-sync delay 10 → 0 ms"]

    seen.clear()
    rec("PRODUCT", "STANDBY")                       # asleep: never read (reports 0)
    assert seen == ["source PRODUCT → STANDBY"] and dsp_seen == {}


def test_bar_event_writes_the_event_file(tmp_path, monkeypatch):
    import logging
    logger = logging.getLogger("soundtouch.events.test")
    logger.propagate = False
    h = logging.FileHandler(tmp_path / "ev.log")
    logger.addHandler(h); logger.setLevel(logging.INFO)
    monkeypatch.setattr(stc, "events", logger)
    stc.bar_event(types.SimpleNamespace(host="10.0.0.5", name="Lounge"), "source A → B")
    h.close()
    assert "Lounge (10.0.0.5)  source A → B" in (tmp_path / "ev.log").read_text()
