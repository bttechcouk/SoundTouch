"""AVTransport metadata: the DIDL-Lite the speaker shows on its display."""
import re
import xml.etree.ElementTree as ET

import soundtouch_controller as stc

NS = {"d": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
      "dc": "http://purl.org/dc/elements/1.1/",
      "upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/"}


def test_didl_is_valid_and_carries_title_and_art():
    root = ET.fromstring(stc.avt_didl("http://h:8888/dlna/stream/x?a=1&b=2",
                                      "Rock & Roll <FM>", "https://img/logo.png?x=1&y=2"))
    item = root.find("d:item", NS)
    assert item.findtext("dc:title", namespaces=NS) == "Rock & Roll <FM>"
    assert item.findtext("upnp:albumArtURI", namespaces=NS) == "https://img/logo.png?x=1&y=2"
    assert item.findtext("upnp:class", namespaces=NS) == "object.item.audioItem.audioBroadcast"
    assert item.findtext("d:res", namespaces=NS) == "http://h:8888/dlna/stream/x?a=1&b=2"


def test_play_via_avt_sends_station_name(monkeypatch, tmp_path):
    monkeypatch.setattr(stc, "STATIONS_DIR", tmp_path)
    stc.PresetStore(stations_dir=tmp_path).save_station("heart", "Heart Hampshire", "https://s/h.mp3", "http://art/h.jpg")
    monkeypatch.setattr(stc.PresetStore.__init__, "__defaults__",
                        (stc.PRESETS_DIR, tmp_path))
    dev = stc.SoundTouchDevice("10.0.0.4")
    sent = []

    class R: status_code = 200; text = ""
    dev._session.post = lambda url, data, headers, timeout: sent.append(data.decode()) or R()
    assert dev.play_via_avt("http://10.0.0.1:8888/dlna/stream/heart")
    soap = ET.fromstring(sent[0])
    meta = soap.find(".//CurrentURIMetaData").text          # unescaped DIDL
    item = ET.fromstring(meta).find("d:item", NS)
    assert item.findtext("dc:title", namespaces=NS) == "Heart Hampshire"
    assert item.findtext("upnp:albumArtURI", namespaces=NS) == "http://art/h.jpg"


def test_unknown_url_sends_empty_metadata():
    dev = stc.SoundTouchDevice("10.0.0.4"); sent = []

    class R: status_code = 200; text = ""
    dev._session.post = lambda url, data, headers, timeout: sent.append(data.decode()) or R()
    dev.play_via_avt("http://example/stream.mp3")
    assert re.search(r"<CurrentURIMetaData></CurrentURIMetaData>", sent[0])
