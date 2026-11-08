"""TTS announcements: speakers without LOCAL_INTERNET_RADIO get the MP3 over AVTransport,
announcements to one speaker never overlap, and the speaker's own volume comes back."""
import threading
import time
import xml.etree.ElementTree as ET

import soundtouch_controller as stc


class FakeDev:
    def __init__(self, host, has_lir, vol=25):
        self.host, self.has_lir, self.vol = host, has_lir, vol
        self.calls, self.playing = [], False

    def _get(self, path):
        if path == "/now_playing":
            return ET.fromstring(f'<nowPlaying source="{"UPNP" if self.playing else "STANDBY"}">'
                                 f'<playStatus>{"PLAY_STATE" if self.playing else ""}</playStatus></nowPlaying>')
        if path == "/volume":
            return ET.fromstring(f"<volume><actualvolume>{self.vol}</actualvolume></volume>")

    def set_volume(self, v): self.calls.append(("vol", v)); self.vol = v
    def has_local_internet_radio(self): return self.has_lir
    def select_content(self, *a): self.calls.append(("select", a[0])); self.playing = True
    def play_via_avt(self, url, title=""): self.calls.append(("avt", url, title)); self.playing = True
    def power(self): self.calls.append(("power",)); self.playing = False
    def _post(self, *a): self.calls.append(("post",))


def run(monkeypatch, devs):
    class G:
        def __init__(self, *a, **k): pass
        def write_to_fp(self, buf): buf.write(b"x" * 1600)
    monkeypatch.setattr(stc, "_TTS_AVAILABLE", True)
    monkeypatch.setattr(stc, "_gTTS", G, raising=False)
    monkeypatch.setattr(stc, "get_local_ip", lambda: "10.0.0.1")
    monkeypatch.setattr(stc.time, "sleep", lambda s: None)
    stc._tts_announce(devs, "hello", 15, 8888)


def test_kitchen_like_speaker_plays_mp3_over_avt(monkeypatch):
    d = FakeDev("10.0.0.6", has_lir=False)
    run(monkeypatch, [d])
    avt = [c for c in d.calls if c[0] == "avt"]
    assert avt and avt[0][1].startswith("http://10.0.0.1:8888/api/tts/audio/") and avt[0][1].endswith(".mp3")
    assert not [c for c in d.calls if c[0] == "select"]
    assert d.calls[0] == ("vol", 15) and ("vol", 25) in d.calls and d.calls[-1] == ("power",)


def test_normal_speaker_still_uses_local_internet_radio(monkeypatch):
    d = FakeDev("10.0.0.7", has_lir=True)
    run(monkeypatch, [d])
    assert ("select", "LOCAL_INTERNET_RADIO") in d.calls and not [c for c in d.calls if c[0] == "avt"]


def test_overlapping_announcements_restore_the_real_volume(monkeypatch):
    d = FakeDev("10.0.0.8", has_lir=False, vol=25)
    t = [threading.Thread(target=run, args=(monkeypatch, [d])) for _ in range(2)]
    for x in t: x.start()
    for x in t: x.join()
    assert d.vol == 25                                  # never left at the announcement volume
    assert [c for c in d.calls if c[0] == "vol"] == [("vol", 15), ("vol", 25), ("vol", 15), ("vol", 25)]
