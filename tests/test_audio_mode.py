"""Dialogue mode: transition rules, /audiodspcontrols parsing, support probe
and the per-speaker auto setting. No speaker hardware — _get / _post / the
HTTP session are stubbed."""
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


# ── TV parking ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("prev, cur, want", [
    ("SPOTIFY",   "STANDBY", True),    # remote / Alexa / idle timer while on music
    ("BLUETOOTH", "STANDBY", True),
    ("PRODUCT",   "STANDBY", False),   # already on TV — wakes on TV anyway
    ("STANDBY",   "STANDBY", False),
    ("SPOTIFY",   "PRODUCT", False),   # our own park switching to TV
    (None,        "STANDBY", False),   # first sighting never acts
])
def test_should_park_on_tv(prev, cur, want):
    assert stc.should_park_on_tv(prev, cur) is want


class _FakeBar:
    def __init__(self, source, tv=True):
        self.host, self.source, self.tv, self.keys = "10.0.0.9", source, tv, []
    def has_tv_input(self): return self.tv
    def current_source(self): return self.source
    def power(self): self.keys.append("POWER")


def _app():
    import threading, types
    app = types.SimpleNamespace(parked=[])
    app.power_off_soundbar = lambda dev, reason: app.parked.append(dev.host) or True
    app.soundbar_power = lambda dev: stc.AppState.soundbar_power(app, dev)
    return app


@pytest.mark.parametrize("source, tv, parks", [
    ("SPOTIFY", True,  True),    # soundbar on music → park on TV first
    ("PRODUCT", True,  False),   # soundbar on TV → plain power-off
    ("STANDBY", True,  False),   # soundbar off → plain power-on
    ("SPOTIFY", False, False),   # ordinary speaker → never parks
])
def test_power_button(source, tv, parks):
    app, bar = _app(), _FakeBar(source, tv)
    assert app.soundbar_power(bar) is parks
    assert (app.parked == [bar.host]) is parks
    assert (bar.keys == ["POWER"]) is (not parks)
