"""Soundbar settings: AV delay, auto-off, HDMI-CEC, attached speakers — all
gated on /capabilities so nothing is probed blind. _get / _post are stubbed."""
import xml.etree.ElementTree as ET

import soundtouch_controller as stc

ST300 = {
    "/capabilities": ('<capabilities deviceID="X">'
                      '<capability name="audiodspcontrols" url="/audiodspcontrols" info="" />'
                      '<capability name="systemtimeoutcontrol" url="/systemtimeoutcontrol" info="" />'
                      '<capability name="productcechdmicontrol" url="/productcechdmicontrol" info="" />'
                      '<capability name="audiospeakerattributeandsetting" '
                      'url="/audiospeakerattributeandsetting" info="" /></capabilities>'),
    "/audiodspcontrols": ('<audiodspcontrols audiomode="AUDIO_MODE_NORMAL" videosyncaudiodelay="40" '
                          'supportedaudiomodes="AUDIO_MODE_NORMAL|AUDIO_MODE_DIALOG" />'),
    "/systemtimeoutcontrol": '<systemtimeoutcontrol autopowerdown="true" screensaver="false" />',
    "/productcechdmicontrol": '<productcechdmicontrol cecmode="CEC_MODE_OFF" />',
    "/audiospeakerattributeandsetting": (
        '<audiospeakerattributeandsetting>'
        '<rear available="true" active="true" wireless="true" controllable="true" />'
        '<subwoofer01 available="false" active="false" wireless="false" controllable="true" />'
        '</audiospeakerattributeandsetting>'),
}
ST20 = {"/capabilities": '<capabilities deviceID="X"><clockDisplay>false</clockDisplay></capabilities>'}


def _dev(responses):
    dev = stc.SoundTouchDevice("192.168.1.60")
    dev.gets, dev.posts = [], []
    def _get(path, timeout=4):
        dev.gets.append(path)
        return ET.fromstring(responses[path]) if path in responses else None
    dev._get = _get
    dev._post = lambda path, body, timeout=4: dev.posts.append((path, body)) or True
    return dev


def test_reads_supported_settings():
    assert _dev(ST300).get_soundbar_settings() == {
        "av_delay": 40, "auto_off": True, "cec": False, "rear": True, "subwoofer": False}


def test_regular_speaker_reads_nothing_but_capabilities():
    dev = _dev(ST20)
    assert dev.get_soundbar_settings() == {}
    assert dev.gets == ["/capabilities"]
    assert dev.get_audio_controls("tone") is None      # no 404 probe either
    assert dev.gets == ["/capabilities"]


def test_av_delay_is_clamped():
    dev = _dev(ST300)
    assert dev.set_soundbar_setting("av_delay", 1000)
    assert dev.posts[-1] == ("/audiodspcontrols", '<audiodspcontrols videosyncaudiodelay="300"/>')
    dev.set_soundbar_setting("av_delay", -5)
    assert 'videosyncaudiodelay="0"' in dev.posts[-1][1]


def test_switches():
    dev = _dev(ST300)
    dev.set_soundbar_setting("auto_off", "false")
    assert dev.posts[-1] == ("/systemtimeoutcontrol", '<systemtimeoutcontrol autopowerdown="false"/>')
    dev.set_soundbar_setting("cec", "true")
    assert dev.posts[-1] == ("/productcechdmicontrol", '<productcechdmicontrol cecmode="CEC_MODE_ON"/>')


def test_unsupported_setting_is_refused():
    dev = _dev(ST20)
    assert dev.set_soundbar_setting("cec", "true") is False
    assert dev.set_soundbar_setting("bogus", "1") is False
    assert dev.posts == []
