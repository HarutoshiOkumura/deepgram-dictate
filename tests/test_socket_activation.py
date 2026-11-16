"""The real daemon process under systemd-style socket activation."""

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not shutil.which("systemd-socket-activate"), reason="needs systemd-socket-activate"
)


def _status(path: Path) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(10)
        s.connect(str(path))
        s.sendall(b'{"cmd": "status"}\n')
        return s.recv(4096)


def test_socket_survives_daemon_exit(tmp_path: Path) -> None:
    """systemd owns the socket: a daemon exit must not delete it, or the hotkey goes dead."""
    path = tmp_path / "deepgram-dictate.sock"
    env = {
        **os.environ,
        "XDG_RUNTIME_DIR": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }
    proc = subprocess.Popen(
        ["systemd-socket-activate", "-l", str(path), sys.executable, "-m",
         "deepgram_dictate.cli", "daemon"],
        env=env,
        stderr=subprocess.PIPE,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 10
        while not path.exists():
            assert time.monotonic() < deadline, "socket never appeared"
            time.sleep(0.05)
        assert b'"state": "idle"' in _status(path)  # first connection starts the daemon
        proc.send_signal(signal.SIGTERM)
        proc.wait(10)
        assert path.exists()
    finally:
        proc.kill()
        proc.wait()
