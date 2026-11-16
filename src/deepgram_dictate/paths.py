"""Filesystem locations, following the XDG base directory spec."""

from __future__ import annotations

import os
from pathlib import Path

APP = "deepgram-dictate"


def _xdg(var: str, fallback: Path) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else fallback


def runtime_dir() -> Path:
    return _xdg("XDG_RUNTIME_DIR", Path(f"/run/user/{os.getuid()}"))


SOCKET_ENV = "DEEPGRAM_DICTATE_SOCKET"


def socket_path() -> Path:
    """The daemon's control socket. DEEPGRAM_DICTATE_SOCKET overrides it (for test instances)."""
    override = os.environ.get(SOCKET_ENV)
    return Path(override) if override else runtime_dir() / f"{APP}.sock"


def config_path() -> Path:
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config") / APP / "config.toml"


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", Path.home() / ".cache") / APP
