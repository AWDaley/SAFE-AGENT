"""Finite UDS HTTP I/O and a real slow-header deadline, no public network."""
from __future__ import annotations

import socket
import threading
import time

import pytest

from safe_agent.inference.coordinated_uds import _Connection


def test_trickling_headers_do_not_extend_absolute_deadline(tmp_path):
    path = tmp_path / "test.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    def slow():
        peer, _ = server.accept()
        try:
            peer.recv(4096)
            for byte in b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n":
                try:
                    peer.send(bytes([byte]))
                except OSError:
                    break
                time.sleep(0.03)
        finally:
            peer.close()
            server.close()
    thread = threading.Thread(target=slow)
    thread.start()
    started = time.monotonic()
    conn = _Connection("inference.example.com", path, started + 0.2)
    try:
        conn.request("POST", "/v1/dispatch", body=b"{}")
        with pytest.raises((TimeoutError, OSError)):
            conn.getresponse()
        assert time.monotonic() - started < 1.0
    finally:
        conn.close()
        thread.join(timeout=3)
    assert not thread.is_alive()
