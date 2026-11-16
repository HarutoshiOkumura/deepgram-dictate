"""Deepgram API key storage in the desktop keyring (Secret Service, via secret-tool).

The key never touches a plain file. DEEPGRAM_API_KEY in the environment is honoured as a
fallback, which is handy for tests and for systems without a keyring.
"""

from __future__ import annotations

import asyncio
import os
import subprocess

_ATTRS = ("service", "deepgram-dictate", "account", "api-key")
_LABEL = "Deepgram API key (deepgram-dictate)"


class MissingKeyError(Exception):
    def __init__(self) -> None:
        super().__init__("No Deepgram API key stored. Run `dictate set-key` in a terminal.")


async def lookup() -> str:
    proc = await asyncio.create_subprocess_exec(
        "secret-tool",
        "lookup",
        *_ATTRS,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    key = out.decode().strip() if proc.returncode == 0 else ""
    key = key or os.environ.get("DEEPGRAM_API_KEY", "").strip()
    if not key:
        raise MissingKeyError
    return key


def store(key: str) -> None:
    subprocess.run(
        ["secret-tool", "store", f"--label={_LABEL}", *_ATTRS],
        input=key.encode(),
        check=True,
    )


class KeyCache:
    """Looks the key up once, and again after Deepgram rejects it (it may have been replaced)."""

    def __init__(self) -> None:
        self._key: str | None = None

    async def get(self) -> str:
        if self._key is None:
            self._key = await lookup()
        return self._key

    def invalidate(self) -> None:
        self._key = None
