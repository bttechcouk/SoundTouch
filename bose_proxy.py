#!/usr/bin/env python3
"""
Bose MARGE WebSocket proxy.

Intercepts the Kitchen speaker's connection to streaming.bose.com,
proxies all traffic transparently, and injects LOCAL_INTERNET_RADIO
into the sources/services configuration when Bose sends it.

Usage:
  python3 bose_proxy.py

Prerequisites:
  1. DNS: on your router, add a custom DNS record:
        streaming.bose.com → 10.10.10.111   (this machine)

  2. Port redirect (run once on this machine):
        sudo iptables -t nat -A PREROUTING -p tcp --dport 443 -j REDIRECT --to-ports 8443
        sudo iptables -t nat -A PREROUTING -p tcp --dport 80  -j REDIRECT --to-ports 8080_bose
     (we handle both ports)

  Then restart the Kitchen speaker and watch the log.
"""

import base64
import hashlib
import logging
import re
import select
import socket
import ssl
import struct
import threading
import time

LISTEN_PORT_TLS   = 8443   # iptables redirects 443 → here
LISTEN_PORT_PLAIN = 8444   # iptables redirects  80 → here (if needed)
BOSE_HOST         = "streaming.bose.com"
BOSE_PORT_TLS     = 443
BOSE_PORT_PLAIN   = 80
CERT_FILE         = "data/ssl/bose_cert.pem"
KEY_FILE          = "data/ssl/bose_key.pem"
LOG_FILE          = "bose_proxy.log"

# ── logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("bose_proxy")

# ── WebSocket helpers ─────────────────────────────────────────────────────────

def _ws_handshake_response(key_b64):
    magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    digest = hashlib.sha1((key_b64 + magic).encode()).digest()
    return base64.b64encode(digest).decode()


def read_http_headers(sock):
    """Read HTTP headers from a socket, return (first_line, headers_dict, raw_bytes)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("Connection closed during HTTP headers")
        buf += chunk
    raw, _, rest = buf.partition(b"\r\n\r\n")
    lines = raw.decode("utf-8", errors="replace").split("\r\n")
    first = lines[0]
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    return first, headers, rest


def ws_recv_frame(sock):
    """Read one WebSocket frame. Returns (opcode, payload_bytes) or (None, None) on close."""
    try:
        h = b""
        while len(h) < 2:
            chunk = sock.recv(2 - len(h))
            if not chunk:
                return None, None
            h += chunk
        fin_opcode = h[0]
        masked_len = h[1]
        opcode = fin_opcode & 0x0F
        masked = bool(masked_len & 0x80)
        length = masked_len & 0x7F

        if length == 126:
            length = struct.unpack(">H", _recv_exactly(sock, 2))[0]
        elif length == 127:
            length = struct.unpack(">Q", _recv_exactly(sock, 8))[0]

        mask_key = _recv_exactly(sock, 4) if masked else b"\x00\x00\x00\x00"
        payload = bytearray(_recv_exactly(sock, length))
        if masked:
            for i in range(len(payload)):
                payload[i] ^= mask_key[i % 4]
        return opcode, bytes(payload)
    except Exception:
        return None, None


def _recv_exactly(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Socket closed")
        buf += chunk
    return buf


def ws_send_frame(sock, payload, opcode=0x01, masked=False):
    """Send a WebSocket text frame."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    length = len(payload)
    frame = bytearray()
    frame.append(0x80 | opcode)  # FIN + opcode
    mask_bit = 0x80 if masked else 0x00
    if length < 126:
        frame.append(mask_bit | length)
    elif length < 65536:
        frame.append(mask_bit | 126)
        frame += struct.pack(">H", length)
    else:
        frame.append(mask_bit | 127)
        frame += struct.pack(">Q", length)
    if masked:
        mask = b"\x00\x00\x00\x00"
        frame += mask
        frame += payload
    else:
        frame += payload
    sock.sendall(bytes(frame))


# ── source injection ──────────────────────────────────────────────────────────

_LOCAL_IR_PATTERNS = [
    b"LOCAL_INTERNET_RADIO",
    b"localInternetRadio",
    b"internetRadio",
    b"InternetRadio",
]

_LIR_XML_SNIPPET = '<sourceItem source="LOCAL_INTERNET_RADIO" sourceAccount="" status="READY" isLocal="false" multiroomallowed="true">Internet Radio</sourceItem>'

_LIR_JSON_SNIPPET = '{"source":"LOCAL_INTERNET_RADIO","sourceAccount":"","status":"READY","isLocal":false}'


def inject_local_internet_radio(payload: bytes) -> bytes:
    """
    If this payload looks like a sources/services message that doesn't
    already include LOCAL_INTERNET_RADIO, inject it.
    Returns (possibly modified) payload.
    """
    if any(pat in payload for pat in _LOCAL_IR_PATTERNS):
        return payload  # already has it

    text = payload.decode("utf-8", errors="replace")

    # XML sources list
    if "<sources" in text or "<sourceItem" in text or "<sourcesUpdated" in text:
        log.info("[INJECT] Found sources XML — injecting LOCAL_INTERNET_RADIO")
        # Insert before closing </sources> tag
        if "</sources>" in text:
            text = text.replace("</sources>", _LIR_XML_SNIPPET + "</sources>")
            return text.encode("utf-8")

    # JSON sources list
    if '"sources"' in text or '"sourceItem"' in text:
        log.info("[INJECT] Found sources JSON — injecting LOCAL_INTERNET_RADIO")
        # Try to find a sources array
        m = re.search(r'("sources"\s*:\s*\[)', text)
        if m:
            pos = m.end()
            text = text[:pos] + _LIR_JSON_SNIPPET + "," + text[pos:]
            return text.encode("utf-8")

    return payload


# ── proxy connection ──────────────────────────────────────────────────────────

def proxy_websocket(client_sock, use_tls):
    """
    1. Complete WebSocket handshake with the client (speaker).
    2. Open a connection to the real streaming.bose.com.
    3. Forward WebSocket frames bidirectionally, injecting LOCAL_INTERNET_RADIO
       into any sources message coming from Bose.
    """
    try:
        first_line, headers, leftover = read_http_headers(client_sock)
        log.info(f"[CLIENT] {first_line}")
        log.debug(f"[CLIENT] Headers: {headers}")

        ws_key = headers.get("sec-websocket-key", "")
        ws_proto = headers.get("sec-websocket-protocol", "gabbo")
        accept = _ws_handshake_response(ws_key)

        client_sock.sendall((
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            f"Sec-WebSocket-Protocol: {ws_proto}\r\n"
            "\r\n"
        ).encode())

        # Connect to real Bose
        raw_bose = socket.create_connection((BOSE_HOST, BOSE_PORT_TLS), timeout=10)
        ctx = ssl.create_default_context()
        bose_sock = ctx.wrap_socket(raw_bose, server_hostname=BOSE_HOST)

        # Forward the original upgrade request to Bose
        bose_sock.sendall((
            f"{first_line}\r\n"
            + "".join(f"{k}: {v}\r\n" for k, v in {
                "Host": BOSE_HOST,
                "Upgrade": "websocket",
                "Connection": "Upgrade",
                "Sec-WebSocket-Key": ws_key,
                "Sec-WebSocket-Version": headers.get("sec-websocket-version", "13"),
                "Sec-WebSocket-Protocol": ws_proto,
            }.items())
            + "\r\n"
        ).encode())
        bose_resp = b""
        while b"\r\n\r\n" not in bose_resp:
            bose_resp += bose_sock.recv(4096)
        log.info(f"[BOSE] Upgrade response: {bose_resp[:200]}")

        log.info("[PROXY] WebSocket tunnel established — proxying frames")

        injected = False

        def client_to_bose():
            while True:
                op, data = ws_recv_frame(client_sock)
                if op is None:
                    break
                log.debug(f"[C→B] op={op} len={len(data)} {data[:120]}")
                ws_send_frame(bose_sock, data, opcode=op)

        def bose_to_client():
            nonlocal injected
            while True:
                op, data = ws_recv_frame(bose_sock)
                if op is None:
                    break
                log.debug(f"[B→C] op={op} len={len(data)} {data[:200]}")
                if not injected and op == 0x01:  # text frame
                    modified = inject_local_internet_radio(data)
                    if modified is not data:
                        injected = True
                        log.info(f"[INJECT] Sending modified payload ({len(modified)} bytes)")
                    data = modified
                ws_send_frame(client_sock, data, opcode=op)

        t1 = threading.Thread(target=client_to_bose, daemon=True)
        t2 = threading.Thread(target=bose_to_client, daemon=True)
        t1.start(); t2.start()
        t1.join(); t2.join()

    except Exception as e:
        log.error(f"[PROXY] Error: {e}")
    finally:
        try: client_sock.close()
        except Exception: pass


# ── server ────────────────────────────────────────────────────────────────────

def serve(port, use_tls):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("", port))
    srv.listen(5)
    log.info(f"[SERVER] Listening on :{port} ({'TLS' if use_tls else 'plain'})")

    while True:
        try:
            conn, addr = srv.accept()
            log.info(f"[SERVER] Connection from {addr[0]}:{addr[1]}")
            if use_tls:
                try:
                    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    ctx.load_cert_chain(CERT_FILE, KEY_FILE)
                    ctx.check_hostname = False
                    conn = ctx.wrap_socket(conn, server_side=True)
                except ssl.SSLError as e:
                    log.warning(f"[TLS] Handshake failed from {addr}: {e}")
                    conn.close()
                    continue
            threading.Thread(
                target=proxy_websocket, args=(conn, use_tls), daemon=True
            ).start()
        except Exception as e:
            log.error(f"[SERVER] Accept error: {e}")


if __name__ == "__main__":
    log.info("=== Bose MARGE proxy starting ===")
    log.info(f"Listening on TLS:{LISTEN_PORT_TLS}  plain:{LISTEN_PORT_PLAIN}")
    log.info(f"Bose upstream: {BOSE_HOST}:{BOSE_PORT_TLS}")
    log.info("")
    log.info("Steps:")
    log.info(f"  1. On router: set streaming.bose.com DNS → 10.10.10.111")
    log.info(f"  2. On this machine (one time):")
    log.info(f"       sudo iptables -t nat -A PREROUTING -p tcp --dport 443 -j REDIRECT --to-ports {LISTEN_PORT_TLS}")
    log.info(f"       sudo iptables -t nat -A PREROUTING -p tcp --dport  80 -j REDIRECT --to-ports {LISTEN_PORT_PLAIN}")
    log.info(f"  3. Restart the Kitchen speaker")
    log.info("")

    t_tls   = threading.Thread(target=serve, args=(LISTEN_PORT_TLS,   True),  daemon=True)
    t_plain = threading.Thread(target=serve, args=(LISTEN_PORT_PLAIN,  False), daemon=True)
    t_tls.start()
    t_plain.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Stopping.")
