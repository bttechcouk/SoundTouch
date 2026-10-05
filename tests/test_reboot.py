"""SoundTouchDevice.reboot() against a fake TAP console on a local port."""
import socket
import threading

import soundtouch_controller as stc


def _fake_console(reply):
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    got = []
    def serve():
        c, _ = srv.accept()
        c.sendall(b"->")
        got.append(c.recv(256))
        c.sendall(reply); c.close(); srv.close()
    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1], got


def _dev(port, monkeypatch):
    real = socket.create_connection
    monkeypatch.setattr(stc.socket, "create_connection",
                        lambda addr, timeout=None: real(("127.0.0.1", port), timeout=timeout))
    return stc.SoundTouchDevice("10.0.0.5")


def test_reboot_sends_only_sys_reboot(monkeypatch):
    port, got = _fake_console(b"Rebooting system\r\n->OK\r\n->")
    assert _dev(port, monkeypatch).reboot() is True
    assert got == [b"sys reboot\r\n"]


def test_reboot_reports_failure_when_not_confirmed(monkeypatch):
    port, _ = _fake_console(b"Command not found\r\n->")
    assert _dev(port, monkeypatch).reboot() is False


def test_reboot_unreachable_is_false(monkeypatch):
    def refuse(addr, timeout=None): raise ConnectionRefusedError()
    monkeypatch.setattr(stc.socket, "create_connection", refuse)
    assert stc.SoundTouchDevice("10.0.0.5").reboot() is False
