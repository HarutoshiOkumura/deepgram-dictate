"""Talking to the daemon. Standard library only: hotkey commands must start in milliseconds."""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Iterator

from . import paths

# The daemon may outlive a compositor restart, so every client tells it the current session.
FORWARDED_ENV = ("WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE", "DISPLAY")


def _connect(timeout: float | None) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(paths.socket_path()))
    except OSError:
        sock.close()
        raise
    return sock


def _message(cmd: str, **fields: object) -> bytes:
    env = {k: os.environ[k] for k in FORWARDED_ENV if k in os.environ}
    return json.dumps({"cmd": cmd, "env": env, **fields}).encode() + b"\n"


def request(cmd: str, timeout: float = 10.0, **fields: object) -> dict:
    """Send one command and return the daemon's reply.

    The timeout is generous because with socket activation the first command also waits for
    the daemon to start.
    """
    with _connect(timeout) as sock:
        sock.sendall(_message(cmd, **fields))
        with sock.makefile("rb") as f:
            line = f.readline()
    if not line:
        raise ConnectionError("the dictation daemon closed the connection without replying")
    return json.loads(line)


def follow() -> Iterator[dict]:
    """Yield the daemon's state every time it changes, until the connection drops."""
    with _connect(None) as sock:
        sock.sendall(_message("subscribe"))
        with sock.makefile("rb") as f:
            for line in f:
                yield json.loads(line)
