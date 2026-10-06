"""Save what's playing as a preset: reading now_playing, which speakers can
take it, and XML-safe storePreset bodies."""
import xml.etree.ElementTree as ET

import soundtouch_controller as stc

SPOTIFY_NP = """<nowPlaying source="SPOTIFY" sourceAccount="turnerben37">
<ContentItem source="SPOTIFY" type="tracklisturl" location="/playback/container/abc"
  sourceAccount="turnerben37" isPresetable="true"><itemName>Morning Wake-Up Indie Mix</itemName></ContentItem>
<track>doing my best</track><art artImageStatus="IMAGE_PRESENT">https://i.scdn.co/image/x</art>
<playStatus>PLAY_STATE</playStatus></nowPlaying>"""


def _dev(np_xml):
    d = stc.SoundTouchDevice("10.0.0.9")
    d._get = lambda p, timeout=4: ET.fromstring(np_xml) if np_xml else None
    return d


def test_now_playing_item_spotify_playlist():
    assert _dev(SPOTIFY_NP).now_playing_item() == {
        "source": "SPOTIFY", "type": "tracklisturl", "location": "/playback/container/abc",
        "account": "turnerben37", "name": "Morning Wake-Up Indie Mix",
        "art": "https://i.scdn.co/image/x"}


def test_nothing_presettable():
    assert _dev('<nowPlaying source="STANDBY"><ContentItem source="STANDBY" isPresetable="false"/></nowPlaying>').now_playing_item() is None
    assert _dev('<nowPlaying source="PRODUCT"><ContentItem source="PRODUCT" sourceAccount="TV" isPresetable="false"/></nowPlaying>').now_playing_item() is None
    assert _dev(None).now_playing_item() is None


def _src(source, account="", status="READY"):
    return {"source": source, "sourceAccount": account, "status": status}


SPOT = {"source": "SPOTIFY", "account": "turnerben37"}


def test_spotify_needs_same_account_ready():
    assert stc.preset_target_ok(SPOT, [_src("SPOTIFY", "turnerben37")]) == (True, "")
    assert stc.preset_target_ok(SPOT, [_src("SPOTIFY", "SpotifyConnectUserName", "UNAVAILABLE")])[0] is False  # the soundbar
    assert stc.preset_target_ok(SPOT, [_src("SPOTIFY", "someoneelse")])[0] is False


def test_upnp_radio_goes_anywhere_and_others_need_the_source():
    assert stc.preset_target_ok({"source": "UPNP"}, []) == (True, "")
    assert stc.preset_target_ok({"source": "TUNEIN"}, [_src("BLUETOOTH")]) == (False, "no TUNEIN source")
    assert stc.preset_target_ok({"source": "TUNEIN"}, [_src("TUNEIN")]) == (True, "")
    assert stc.preset_target_ok(SPOT, []) == (False, "unreachable")


def test_store_preset_escapes_and_adds_art():
    d = stc.SoundTouchDevice("10.0.0.9"); sent = []
    d._post = lambda path, body, timeout=4: sent.append(body) or True
    d.store_preset(4, "Rock & Roll <Hits>", "SPOTIFY", "tracklisturl", "/x?a=1&b=2", "acct", "https://img/1&2")
    root = ET.fromstring(sent[0])               # must be well-formed XML
    ci = root.find("ContentItem")
    assert root.get("id") == "4" and ci.get("location") == "/x?a=1&b=2"
    assert ci.findtext("itemName") == "Rock & Roll <Hits>"
    assert ci.findtext("containerArt") == "https://img/1&2"
