"""`dictate doctor`: check every piece the dictation pipeline depends on."""

from __future__ import annotations

import asyncio
import json
import shutil
import time

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from . import client, credentials
from . import config as config_mod
from .audio import source_status
from .config import Config, ConfigError
from .deepgram import stream_url

_TOOLS = {
    "pw-record": "pipewire-utils",
    "pw-play": "pipewire-utils",
    "pactl": "pulseaudio-utils",
    "wtype": "wtype",
    "wl-copy": "wl-clipboard",
    "notify-send": "libnotify",
    "secret-tool": "libsecret",
    "hyprctl": "hyprland",
}


async def check_key(key: str, cfg: Config | None = None) -> tuple[bool, str]:
    """Open (and immediately close) a streaming connection. No audio is sent or billed."""
    cfg = cfg or Config()
    started = time.monotonic()
    try:
        ws = await connect(
            stream_url(cfg.profile(None), cfg),
            additional_headers={"Authorization": f"Token {key}"},
            open_timeout=8,
        )
    except InvalidStatus as e:
        return False, f"HTTP {e.response.status_code}"
    except (OSError, TimeoutError) as e:
        return False, f"network error: {e}"
    await ws.send(json.dumps({"type": "CloseStream"}))
    await ws.close()
    return True, f"connected in {(time.monotonic() - started) * 1000:.0f} ms"


def _line(ok: bool | None, label: str, detail: str = "") -> None:
    mark = {True: "\033[32m✔\033[0m", False: "\033[31m✘\033[0m", None: "\033[33m!\033[0m"}[ok]
    print(f" {mark} {label}" + (f": {detail}" if detail else ""))


async def _run() -> int:
    problems = 0

    print("Tools")
    for tool, package in _TOOLS.items():
        found = shutil.which(tool)
        _line(bool(found), tool, found or f"missing, install with: sudo dnf install {package}")
        problems += not found

    print("Configuration")
    try:
        cfg = config_mod.load()
        _line(True, "config", f"profiles: {', '.join(sorted(cfg.profiles))}")
    except ConfigError as e:
        _line(False, "config", str(e))
        return problems + 1

    print("Deepgram")
    try:
        key = await credentials.lookup()
    except credentials.MissingKeyError as e:
        _line(False, "API key", str(e))
        problems += 1
    else:
        ok, detail = await check_key(key, cfg)
        _line(ok, "API key", detail)
        problems += not ok

    print("Microphone")
    status = await source_status(cfg.audio_source)
    source = cfg.audio_source or "system default"
    detail = {
        "muted": "MUTED, press the mic-mute key",
        "unmuted": "unmuted",
        "missing": "does not exist (see `pactl list short sources`)",
        "unknown": "could not read its state",
    }[status]
    _line(None if status == "unknown" else status == "unmuted", source, detail)
    problems += status in ("muted", "missing")

    print("Daemon")
    try:
        reply = client.request("status", timeout=10)
        _line(reply.get("ok", False), "deepgram-dictate", f"state: {reply.get('state')}")
    except OSError as e:
        _line(False, "deepgram-dictate", f"not reachable ({e})")
        problems += 1

    print("Hyprland")
    try:
        proc = await asyncio.create_subprocess_exec(
            "hyprctl", "binds", "-j", stdout=asyncio.subprocess.PIPE
        )
        binds, _ = await asyncio.wait_for(proc.communicate(), 5)
        bound = [b for b in json.loads(binds) if "dictate" in b.get("arg", "")]
        # Multi-key binds (Right Alt + C) report an empty key, so name binds by description.
        names = [b.get("description") or b.get("key") or b["arg"] for b in bound]
        _line(bool(bound), "hotkeys", f"{len(bound)} loaded" if bound else "none loaded")
        for name in names:
            print(f"     {name}")
        problems += not bound
    except (OSError, TimeoutError, ValueError) as e:
        _line(None, "hotkey", f"could not query Hyprland ({e})")

    print()
    print("All good." if not problems else f"{problems} problem(s) found.")
    return 1 if problems else 0


def main() -> int:
    return asyncio.run(_run())
