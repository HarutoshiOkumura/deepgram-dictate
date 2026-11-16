"""Deliver text to whichever window has keyboard focus."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping

log = logging.getLogger(__name__)

# Terminals paste with Ctrl+Shift+V; Ctrl+V means something else there.
_TERMINAL_CLASSES = {
    "kitty", "foot", "footclient", "alacritty", "com.mitchellh.ghostty", "ghostty",
    "org.wezfurlong.wezterm", "wezterm", "konsole", "org.gnome.console", "org.gnome.ptyxis",
    "gnome-terminal-server", "xterm", "tilix", "terminator", "st", "urxvt",
}  # fmt: skip


class InjectError(Exception):
    pass


async def _run(args: list[str], env: Mapping[str, str], stdin: bytes | None = None) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *args,
        env=dict(env),
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(stdin)
    if proc.returncode != 0:
        raise InjectError(f"{args[0]} failed: {err.decode(errors='replace').strip()}")
    return out


async def type_text(text: str, env: Mapping[str, str]) -> None:
    """Type through a Wayland virtual keyboard. Handles any Unicode, needs no clipboard."""
    await _run(["wtype", "-"], env, stdin=text.encode())


async def _active_window_class(env: Mapping[str, str]) -> str:
    try:
        info = json.loads(await _run(["hyprctl", "activewindow", "-j"], env))
        return str(info.get("class", "")).lower()
    except (InjectError, ValueError, OSError):
        return ""


async def _copy(data: bytes, env: Mapping[str, str]) -> None:
    """Set the clipboard. wl-copy forks a child that keeps serving the clipboard, and that
    child inherits any pipes; so its output must not be piped, or waiting would never end."""
    proc = await asyncio.create_subprocess_exec(
        "wl-copy",
        "--type",
        "text/plain;charset=utf-8",
        env=dict(env),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate(data)
    if proc.returncode != 0:
        raise InjectError(f"wl-copy failed with exit status {proc.returncode}")


async def paste_text(text: str, env: Mapping[str, str]) -> None:
    """Put the text on the clipboard, press the paste shortcut, then restore the clipboard.

    Only a text clipboard can be restored; if it held something else (an image), the
    dictated text stays on the clipboard afterwards.
    """
    previous: bytes | None = None
    try:
        types = (await _run(["wl-paste", "--list-types"], env)).decode().split()
        if "text/plain;charset=utf-8" in types or "text/plain" in types:
            previous = await _run(["wl-paste", "--no-newline", "--type", "text/plain"], env)
    except InjectError:
        previous = None  # empty clipboard

    await _copy(text.encode(), env)
    shift = await _active_window_class(env) in _TERMINAL_CLASSES
    keys = ["-M", "ctrl"] + (["-M", "shift"] if shift else []) + ["-k", "v"]
    keys += (["-m", "shift"] if shift else []) + ["-m", "ctrl"]
    await _run(["wtype", *keys], env)
    # Give the application time to read the clipboard before it changes back.
    await asyncio.sleep(0.4)
    if previous is not None:
        await _copy(previous, env)


async def deliver(text: str, method: str, env: Mapping[str, str]) -> None:
    if not text:
        return
    if method == "paste":
        await paste_text(text, env)
    else:
        await type_text(text, env)
