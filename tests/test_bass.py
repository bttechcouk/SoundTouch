"""Bass: /bass on regular speakers vs the soundbar's /audioproducttonecontrols.
No speaker hardware — _get / _post are stubbed."""
import xml.etree.ElementTree as ET

import soundtouch_controller as stc

ST300_CAPS = ('<bassCapabilities deviceID="X"><bassAvailable>false</bassAvailable>'
              '</bassCapabilities>')
ST300_TONE = ('<audioproducttonecontrols>'
              '<bass value="{}" minValue="-100" maxValue="100" step="25" />'
              '<treble value="-25" minValue="-100" maxValue="100" step="25" />'
              '</audioproducttonecontrols>')
ST20_CAPS = ('<bassCapabilities deviceID="X"><bassAvailable>true</bassAvailable>'
             '<bassMin>-9</bassMin><bassMax>0</bassMax><bassDefault>0</bassDefault>'
             '</bassCapabilities>')


def _dev(responses):
    """Device whose _get returns responses[path] (parsed) and records POSTs."""
    dev = stc.SoundTouchDevice("192.168.1.60")
    dev._get = lambda path, timeout=4: (ET.fromstring(responses[path])
                                        if responses.get(path) else None)
    dev.posts = []
    dev._post = lambda path, body, timeout=4: dev.posts.append((path, body)) or True
    return dev


def test_soundbar_bass_comes_from_tone_controls():
    dev = _dev({"/bassCapabilities": ST300_CAPS,
                "/audioproducttonecontrols": ST300_TONE.format(50)})
    caps = dev.get_bass_capabilities()
    assert caps == {"available": True, "kind": "tone", "min": -100, "max": 100,
                    "step": 25, "default": 0, "current": 50}


def test_soundbar_set_bass_keeps_treble_and_clamps():
    dev = _dev({"/bassCapabilities": ST300_CAPS,
                "/audioproducttonecontrols": ST300_TONE.format(50)})
    dev.get_bass_capabilities()
    dev.set_bass(500)
    path, body = dev.posts[-1]
    assert path == "/audioproducttonecontrols"
    root = ET.fromstring(body)
    assert root.find("bass").get("value") == "100"
    assert root.find("treble").get("value") == "-25"


def test_regular_speaker_uses_bass_endpoint():
    dev = _dev({"/bassCapabilities": ST20_CAPS})
    caps = dev.get_bass_capabilities()
    assert caps["available"] and caps["kind"] == "bass"
    assert (caps["min"], caps["max"]) == (-9, 0)
    dev.set_bass(-4)
    assert dev.posts[-1] == ("/bass", "<bass>-4</bass>")


def test_no_bass_anywhere():
    dev = _dev({"/bassCapabilities": ST300_CAPS})   # tone controls 404
    assert dev.get_bass_capabilities()["available"] is False
