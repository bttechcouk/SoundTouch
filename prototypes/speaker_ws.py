#!/usr/bin/env python3
"""Prototype for #77: listen to a SoundTouch speaker's push notifications
instead of polling it.

Speakers publish state changes (now playing, volume, preset selection, zone…)
over a WebSocket on port 8080, subprotocol "gabbo". This connects to one
speaker and prints every notification with a millisecond timestamp, so the
delay between a physical button press and the notification can be compared
with the controller's 2-second poll.

Standard library only — a deliberately small RFC 6455 client (text frames,
ping/pong, close), so the real thing would not need a new dependency.

    python3 prototypes/speaker_ws.py 10.10.10.128            # until Ctrl-C
    python3 prototypes/speaker_ws.py 10.10.10.128 --seconds 600
"""
import argparse
import base64
import datetime
import os
import socket
import struct
import time
import xml.etree.ElementTree as ET


class SpeakerSocket:
    """Minimal WebSocket client for a speaker's notification channel."""

    def __init__(self, host, port=8080, timeout=10):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: gabbo\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise ConnectionError("speaker closed the connection during the handshake")
            head += chunk
        self.handshake, self._buf = head.split(b"\r\n\r\n", 1)
        if b" 101 " not in self.handshake.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"not a WebSocket upgrade: {self.handshake[:80]!r}")

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("speaker closed the connection")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send(self, opcode, payload=b""):
        mask = os.urandom(4)                       # clients must mask
        head = bytes([0x80 | opcode])
        n = len(payload)
        head += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(self, timeout=None):
        """Next text message, or None on timeout. Answers pings itself."""
        self.sock.settimeout(timeout)
        text = b""
        while True:
            try:
                b0, b1 = self._read(2)
            except socket.timeout:
                return None
            self.sock.settimeout(10)               # a frame has started: finish it
            n = b1 & 0x7F
            if n == 126: n = struct.unpack(">H", self._read(2))[0]
            elif n == 127: n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            opcode = b0 & 0x0F
            if opcode == 0x9:   self._send(0xA, data)          # ping → pong
            elif opcode == 0x8: raise ConnectionError("speaker sent close")
            elif opcode in (0x0, 0x1):
                text += data
                if b0 & 0x80:                                  # FIN
                    return text.decode("utf-8", "replace")
            self.sock.settimeout(timeout)

    def close(self):
        try: self._send(0x8)
        except OSError: pass
        self.sock.close()


def summarise(xml_text):
    """One line saying what a notification is about."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return "unparsed"
    if root.tag != "updates":
        return root.tag
    out = []
    for u in root:
        if u.tag == "nowPlayingUpdated":
            np = u.find("nowPlaying")
            ci = np.find("ContentItem") if np is not None else None
            out.append("nowPlaying source=%s status=%s item=%s" % (
                np.get("source") if np is not None else "?",
                np.findtext("playStatus") if np is not None else "?",
                (ci.findtext("itemName") or ci.get("location") or "") if ci is not None else ""))
        elif u.tag == "volumeUpdated":
            out.append("volume target=%s actual=%s" % (u.findtext("volume/targetvolume"),
                                                        u.findtext("volume/actualvolume")))
        elif u.tag == "nowSelectionUpdated":
            p = u.find("preset")
            out.append("presetSelected id=%s name=%s" % (
                p.get("id") if p is not None else "?",
                p.findtext("ContentItem/itemName") if p is not None else "?"))
        else:
            out.append(u.tag)
    return "; ".join(out) or "updates (empty)"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("host")
    ap.add_argument("--seconds", type=float, default=0, help="stop after this long (0 = until Ctrl-C)")
    ap.add_argument("--raw", action="store_true", help="also print each notification's XML")
    a = ap.parse_args()

    stamp = lambda: datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    ws = SpeakerSocket(a.host)
    print(f"{stamp()}  connected to {a.host}:8080 (gabbo)", flush=True)
    end = time.monotonic() + a.seconds if a.seconds else None
    try:
        while end is None or time.monotonic() < end:
            msg = ws.recv(timeout=1)
            if msg is None:
                continue
            print(f"{stamp()}  {summarise(msg)}", flush=True)
            if a.raw:
                print(f"              {msg}", flush=True)
    except KeyboardInterrupt:
        pass
    except ConnectionError as e:
        print(f"{stamp()}  disconnected: {e}", flush=True)
    finally:
        ws.close()


if __name__ == "__main__":
    main()
