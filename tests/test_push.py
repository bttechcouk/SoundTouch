"""Speaker push notifications: the WebSocket client (against a fake speaker on
localhost), recognising a preset-button press, and playing what it named."""
import base64
import hashlib
import socket
import struct
import threading
import types

import pytest

import soundtouch_controller as stc

# Captured from the Kitchen speaker (ST10, UPnP-only) on 2026-11-12
PRESS = ('<updates deviceID="68C90BBE59EA"><nowSelectionUpdated><preset id="2">'
         '<ContentItem source="UPNP" type="" location="http://10.10.10.111:8888/dlna/stream/kiss" '
         'sourceAccount="UPnPUserName" isPresetable="true"><itemName>KISS</itemName></ContentItem>'
         '</preset></nowSelectionUpdated></updates>')
GAVE_UP = ('<updates deviceID="68C90BBE59EA"><nowSelectionUpdated><preset id="0">'
           '<ContentItem source="INVALID_SOURCE" sourceAccount="" isPresetable="true" />'
           '</preset></nowSelectionUpdated></updates>')
VOLUME = ('<updates deviceID="68C90BBE59EA"><volumeUpdated><volume><targetvolume>29</targetvolume>'
          '<actualvolume>29</actualvolume><muteenabled>false</muteenabled></volume></volumeUpdated></updates>')


# ── preset_press ─────────────────────────────────────────────────────────────
def test_preset_press_names_slot_and_station():
    assert stc.preset_press(PRESS) == ("2", "KISS", "kiss")


@pytest.mark.parametrize("msg", [
    GAVE_UP, VOLUME,
    '<userActivityUpdate deviceID="68C90BBE59EA" />',
    '<SoundTouchSdkInfo serverVersion="4" />',
    PRESS.replace('source="UPNP"', 'source="SPOTIFY"'),             # not ours to play
    PRESS.replace("/dlna/stream/kiss", "/some/other/server.mp3"),   # someone else's UPnP
    PRESS.replace("/dlna/stream/kiss", "/dlna/stream/"),
    "not xml at all",
])
def test_preset_press_ignores_everything_else(msg):
    assert stc.preset_press(msg) is None


# ── SpeakerSocket against a fake speaker ─────────────────────────────────────
def _frame(opcode, payload=b"", fin=True):
    head = bytes([(0x80 if fin else 0) | opcode])
    n = len(payload)
    head += bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n)
    return head + payload


@pytest.fixture
def fake_speaker():
    """A one-connection WebSocket server; `script(conn)` runs after the handshake.
    Yields (port, start) — start(script) must be called before connecting."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0)); srv.listen(1)
    seen = {}

    def start(script):
        def run():
            conn, _ = srv.accept()
            req = b""
            while b"\r\n\r\n" not in req:
                req += conn.recv(1024)
            seen["request"] = req.decode()
            key = [l.split(": ", 1)[1] for l in seen["request"].split("\r\n")
                   if l.lower().startswith("sec-websocket-key")][0]
            accept = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            conn.sendall((f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n"
                          "Sec-WebSocket-Protocol: gabbo\r\n\r\n").encode())
            try: script(conn, seen)
            finally: conn.close()
        threading.Thread(target=run, daemon=True).start()

    yield srv.getsockname()[1], start, seen
    srv.close()


def _read_client_frame(conn):
    b0, b1 = conn.recv(2)
    n = b1 & 0x7F
    mask = conn.recv(4)
    data = conn.recv(n) if n else b""
    return b0 & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def test_socket_reads_messages_and_answers_pings(fake_speaker):
    port, start, seen = fake_speaker
    big = "<updates>" + "x" * 300 + "</updates>"            # 16-bit length frame

    def script(conn, seen):
        conn.sendall(_frame(0x1, b"<SoundTouchSdkInfo/>"))
        conn.sendall(_frame(0x9, b"hi"))                    # ping
        seen["pong"] = _read_client_frame(conn)
        conn.sendall(_frame(0x1, big[:100].encode(), fin=False) + _frame(0x0, big[100:].encode()))
        conn.sendall(_frame(0x8))                           # close

    start(script)
    ws = stc.SpeakerSocket("127.0.0.1", port, timeout=5)
    assert "Sec-WebSocket-Protocol: gabbo" in seen["request"]
    assert ws.recv(timeout=5) == "<SoundTouchSdkInfo/>"
    assert ws.recv(timeout=5) == big                        # fragmented, after the ping
    assert seen["pong"] == (0xA, b"hi")
    with pytest.raises(ConnectionError):
        ws.recv(timeout=5)
    ws.close()


def test_socket_recv_times_out_quietly_and_detects_a_dead_link(fake_speaker):
    port, start, _ = fake_speaker
    gate = threading.Event()
    start(lambda conn, seen: gate.wait(5))                  # say nothing, then hang up
    ws = stc.SpeakerSocket("127.0.0.1", port, timeout=5)
    assert ws.recv(timeout=0.2) is None
    gate.set()
    with pytest.raises(ConnectionError):
        ws.recv(timeout=5)
    ws.close()


def test_socket_rejects_a_non_websocket_answer():
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    def run():
        conn, _ = srv.accept(); conn.recv(1024)
        conn.sendall(b"HTTP/1.1 404 Not Found\r\n\r\n"); conn.close()
    threading.Thread(target=run, daemon=True).start()
    with pytest.raises(ConnectionError):
        stc.SpeakerSocket("127.0.0.1", srv.getsockname()[1], timeout=5)
    srv.close()


# ── acting on a press ────────────────────────────────────────────────────────
def _state(playing_after):
    """A bare AppState-alike plus a fake speaker that reports playing after
    `playing_after` Play commands."""
    plays = []
    dev = types.SimpleNamespace(
        host="10.0.0.8", name="Kitchen", has_tv_input=lambda: False,
        play_via_avt=lambda url: plays.append(url) or True,
        is_playing=lambda: len(plays) >= playing_after)
    app = types.SimpleNamespace(
        dlna=types.SimpleNamespace(stream_url=lambda sid: f"http://ctl:8888/dlna/stream/{sid}"),
        _press_handled={})
    return app, dev, plays


@pytest.fixture
def events(monkeypatch):
    monkeypatch.setattr(stc.time, "sleep", lambda s: None)
    seen = []
    monkeypatch.setattr(stc, "history_event", lambda kind, who, msg: seen.append((kind, who, msg)))
    return seen


def test_press_plays_the_station_that_was_pressed(events):
    app, dev, plays = _state(playing_after=1)
    assert stc.AppState._handle_push(app, dev, PRESS) is True
    # the URL is rebuilt for where the controller is *now*, not the one saved in the preset
    assert plays == ["http://ctl:8888/dlna/stream/kiss"]
    assert "10.0.0.8" in app._press_handled                 # the poll loop stands back
    assert events == [("speaker", "Kitchen", "preset 2 (KISS) pressed — playing")]


def test_press_retries_once_then_reports_failure(events):
    app, dev, plays = _state(playing_after=2)
    assert stc.AppState._handle_push(app, dev, PRESS) is True
    assert len(plays) == 2

    app, dev, plays = _state(playing_after=99)
    assert stc.AppState._handle_push(app, dev, PRESS) is False
    assert len(plays) == 2
    assert events[-1][2] == "preset 2 (KISS) pressed — did not start"


def test_other_notifications_do_nothing(events):
    app, dev, plays = _state(playing_after=1)
    for msg in (GAVE_UP, VOLUME):
        assert stc.AppState._handle_push(app, dev, msg) is None
    assert plays == [] and app._press_handled == {} and events == []


def test_soundbar_presses_are_left_to_the_poll_loop(events):
    app, dev, plays = _state(playing_after=1)
    dev.has_tv_input = lambda: True
    assert stc.AppState._handle_push(app, dev, PRESS) is None
    assert plays == [] and app._press_handled == {} and events == []
