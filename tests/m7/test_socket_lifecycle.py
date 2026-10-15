"""Real socket ownership checks, including abnormal process termination."""
from __future__ import annotations

import os
import socket
import subprocess
import sys

import pytest

from safe_agent.inference_service.socket_lifecycle import OwnedUnixListener


def test_socket_lock_prevents_second_listener_without_unlink(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "inference.sock"
    with OwnedUnixListener(path):
        before = path.stat().st_ino
        with pytest.raises(BlockingIOError):
            with OwnedUnixListener(path):
                pytest.fail("second listener was admitted")
        assert path.stat().st_ino == before
    assert not path.exists()


def test_crash_restarts_only_exact_recorded_socket(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "inference.sock"
    result = subprocess.run([sys.executable, "-c",
        "import os,sys; from pathlib import Path; "
        "from safe_agent.inference_service.socket_lifecycle import OwnedUnixListener; "
        "owner=OwnedUnixListener(Path(sys.argv[1])); owner.__enter__(); os._exit(0)", str(path)],
        capture_output=True, timeout=5, check=False)
    assert result.returncode == 0
    assert path.exists()
    with OwnedUnixListener(path) as listener:
        assert listener.fileno() >= 0
    assert not path.exists()


@pytest.mark.parametrize("kind", ["unknown_socket", "regular_file", "symlink", "public_directory", "forged_journal"])
def test_foreign_or_unsafe_socket_paths_fail_closed(tmp_path, kind):
    tmp_path.chmod(0o700)
    path = tmp_path / "inference.sock"
    peer = None
    if kind in ("unknown_socket", "forged_journal"):
        peer = socket.socket(socket.AF_UNIX)
        peer.bind(str(path))
        path.chmod(0o600)
        if kind == "forged_journal":
            journal = tmp_path / "inference.sock.owner"
            journal.write_text('{"dev":0,"ino":0,"uid":0}')
            journal.chmod(0o600)
    elif kind == "regular_file":
        path.write_text("do not delete")
    elif kind == "symlink":
        path.symlink_to(tmp_path / "other")
    else:
        tmp_path.chmod(0o755)
    try:
        with pytest.raises((ValueError, OSError)):
            with OwnedUnixListener(path):
                pytest.fail("unsafe listener admitted")
        if kind != "public_directory":
            assert os.path.lexists(path), "unowned path was deleted"
    finally:
        if peer is not None:
            peer.close()
